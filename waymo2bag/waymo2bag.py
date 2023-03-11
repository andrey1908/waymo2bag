import argparse
from collections import defaultdict
import glob
import os

import cv2
from geometry_msgs.msg import TransformStamped
import numpy as np
import rospy
from sensor_msgs.msg import Image, PointField, CameraInfo
from nav_msgs.msg import Odometry
import sensor_msgs.point_cloud2 as point_cloud2
from std_msgs.msg import Header
import tensorflow
import tf
from tf2_msgs.msg import TFMessage
import tqdm
from waymo_open_dataset import dataset_pb2
from waymo_open_dataset.utils import frame_utils, range_image_utils, transform_utils

import rosbag

# There is no bounding box annotations in the No Label Zone (NLZ)
# if set True, points in the NLZ are filtered
FILTER_NO_LABEL_ZONE_POINTS = False

# The dataset contains data from five lidars
# one mid-range lidar (top) and four short-range lidars (front, side left, side right, and rear)
SELECTED_LIDAR_SENSOR = {
    dataset_pb2.LaserName.TOP: "top"
    # dataset_pb2.LaserName.FRONT: "front",
    # dataset_pb2.LaserName.SIDE_LEFT: "side_left",
    # dataset_pb2.LaserName.SIDE_RIGHT: "side_right",
    # dataset_pb2.LaserName.REAR: "rear",
}

# The value in the waymo open dataset is the raw intensity
# https://github.com/waymo-research/waymo-open-dataset/issues/93
NORMALIZE_INTENSITY = True

os.environ["CUDA_VISIBLE_DEVICES"] = "-1"


class Waymo2Bag(object):
    def __init__(self, load_dir, save_dir):
        # turn on eager execution for older tensorflow versions
        if int(tensorflow.__version__.split(".")[0]) < 2:
            tensorflow.enable_eager_execution()

        self.load_dir = load_dir
        self.save_dir = save_dir
        self.tfrecord_pathnames = sorted(glob.glob("tfrecord/*.tfrecord"))

        self.static_tf_message = None

    def __len__(self):
        return len(self.tfrecord_pathnames)

    def convert(self):
        print("start converting ...")
        for i in range(len(self)):
            self.convert_tfrecord2bag(i)
            self.static_tf_message = None
        print("finished ...")

    def convert_tfrecord2bag(self, file_idx):
        pathname = self.tfrecord_pathnames[file_idx]
        dataset = tensorflow.data.TFRecordDataset(pathname, compression_type="")
        dataset = list(dataset)

        filename = os.path.basename(pathname).split(".")[0]
        bag = rosbag.Bag(
            "rosbag/" + str(filename) + ".bag", "w", compression=rosbag.Compression.NONE
        )
        print("filename: %s" % str(filename))

        try:
            for frame_idx, data in enumerate(tqdm.tqdm(dataset)):
                frame = dataset_pb2.Frame()
                frame.ParseFromString(bytearray(data.numpy()))

                timestamp = rospy.Time.from_sec(frame.timestamp_micros * 1e-6)
                # self.write_tf(bag, frame, timestamp)
                self.write_odom(bag, frame, timestamp)
                self.write_tf_static(bag, frame, timestamp)
                self.write_point_cloud(bag, frame, timestamp)
                self.write_image(bag, frame, timestamp)
                self.write_camera_info(bag, frame, timestamp)
        finally:
            print(bag)
            bag.close()

    def write_odom(self, bag, frame, timestamp):
        odom_msg = Odometry()
        odom_msg.header.frame_id = "map"
        odom_msg.header.stamp = timestamp
        odom_msg.child_frame_id = "base_link"

        tf_matrix = np.array(frame.pose.transform).reshape(4, 4)
        tf_msg = to_transform(
            from_frame_id="",
            to_frame_id="",
            stamp=rospy.Time(),
            trans_mat=tf_matrix)
        odom_msg.pose.pose.position.x = tf_msg.transform.translation.x
        odom_msg.pose.pose.position.y = tf_msg.transform.translation.y
        odom_msg.pose.pose.position.z = tf_msg.transform.translation.z
        odom_msg.pose.pose.orientation = tf_msg.transform.rotation

        bag.write("/odom", odom_msg, t=timestamp)

    def write_tf_static(self, bag, frame, timestamp):
        tf_message = TFMessage()
        for camera_calibration in frame.context.camera_calibrations:
            frame_name = \
                dataset_pb2.CameraName.Name.Name(camera_calibration.name).lower()
            if frame_name != 'front':
                continue

            vehicle_to_camera = \
                np.array(camera_calibration.extrinsic.transform).reshape(4, 4)
            camera_to_image = np.array(
                [[ 0,  0,  1,  0],
                 [-1,  0,  0,  0],
                 [ 0, -1,  0,  0],
                 [ 0,  0,  0,  1]])
            tf_matrix = np.matmul(vehicle_to_camera, camera_to_image)
            tf_msg = to_transform(
                from_frame_id="base_link",
                to_frame_id=frame_name,
                stamp=rospy.Time(),
                trans_mat=tf_matrix)
            tf_message.transforms.append(tf_msg)
        
        if self.static_tf_message is None:
            bag.write("/tf_static", tf_message, t=timestamp)
            self.static_tf_message = tf_message
        else:
            assert self.static_tf_message == tf_message

    def write_tf(self, bag, frame, timestamp):
        """
        Args:
            bag (rosbag.Bag): bag to write
            frame (waymo_open_dataset.dataset_pb2.Frame): frame info
            timestamp (rospy.rostime.Time): timestamp of a frame
        """

        Tr_vehicle2world = np.array(frame.pose.transform).reshape(4, 4)

        transforms = [
            ("map", "base_link", Tr_vehicle2world),
        ]

        tf_message = TFMessage()
        for transform in transforms:
            _tf_msg = to_transform(
                from_frame_id=transform[0],
                to_frame_id=transform[1],
                stamp=timestamp,
                trans_mat=transform[2],
            )
            tf_message.transforms.append(_tf_msg)

        bag.write("/tf", tf_message, t=timestamp)

    def write_image(self, bag, frame, timestamp):
        """
        Args:
            bag (rosbag.Bag): bag to write
            frame (waymo_open_dataset.dataset_pb2.Frame): frame info
            timestamp (rospy.rostime.Time): timestamp of a frame
        """
        for image in frame.images:
            frame_name = dataset_pb2.CameraName.Name.Name(image.name).lower()
            if frame_name != 'front':
                continue

            img_bgr = cv2.imdecode(np.frombuffer(image.image, np.uint8), cv2.IMREAD_COLOR)
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

            # image_msg = CompressedImage()
            # image_msg.header = Header(frame_id=frame_name, stamp=timestamp)
            # image_msg.format = "jpeg"
            # image_msg.data = np.array(cv2.imencode('.jpg', img_rgb)[1]).tostring()

            image_msg = Image()
            image_msg.header = Header(frame_id=frame_name, stamp=timestamp)
            image_msg.height = img_rgb.shape[0]
            image_msg.width = img_rgb.shape[1]
            image_msg.encoding = "rgb8"
            image_msg.data = img_rgb.tostring()

            bag.write("/camera/{}/image".format(frame_name), image_msg, t=timestamp)

    def write_camera_info(self, bag, frame, timestamp):
        for camera_calibration in frame.context.camera_calibrations:
            frame_name = \
                dataset_pb2.CameraName.Name.Name(camera_calibration.name).lower()
            if frame_name != 'front':
                continue

            cam_info = CameraInfo()
            cam_info.header.frame_id = frame_name
            cam_info.header.stamp = timestamp
            cam_info.width = camera_calibration.width
            cam_info.height = camera_calibration.height
            cam_info.distortion_model = "plumb_bob"
            cam_info.D.extend(camera_calibration.intrinsic[4:])
            K = np.eye(3)
            K[0][0] = camera_calibration.intrinsic[0]
            K[1][1] = camera_calibration.intrinsic[1]
            K[0][2] = camera_calibration.intrinsic[2]
            K[1][2] = camera_calibration.intrinsic[3]
            cam_info.K = K.ravel().tolist()
            R = np.eye(3)
            cam_info.R = R.ravel().tolist()
            P = np.zeros((3, 4))
            P[0:3, 0:3] = K
            cam_info.P = P.ravel().tolist()

            bag.write("/camera/{}/camera_info".format(frame_name), cam_info, t=timestamp)

    def write_point_cloud(self, bag, frame, timestamp):
        """parse and save the lidar data in psd format
        Args:
            bag (rosbag.Bag):
            frame (waymo_open_dataset.dataset_pb2.Frame):
            timestamp (rospy.rostime.Time): timestamp of a frame
        """

        (
            range_images,
            camera_projections,
            _,
            range_image_top_pose,
        ) = frame_utils.parse_range_image_and_camera_projection(frame)
        ret_dict = convert_range_image_to_point_cloud(
            frame, range_images, camera_projections, range_image_top_pose, ri_indexes=(0, 1)
        )

        def write_points_to_bag(points, lidar_name):
            # pointcloud is already transformed to base_link

            fields = [
                PointField("x", 0, PointField.FLOAT32, 1),
                PointField("y", 4, PointField.FLOAT32, 1),
                PointField("z", 8, PointField.FLOAT32, 1),
                PointField("intensity", 12, PointField.FLOAT32, 1),
            ]
            pcl_msg = point_cloud2.create_cloud(
                Header(frame_id="base_link", stamp=timestamp), fields, points
            )

            bag.write("/lidar/{}/pointcloud".format(lidar_name), pcl_msg, t=pcl_msg.header.stamp)

        concat_points = []
        for lidar_id, lidar_name in SELECTED_LIDAR_SENSOR.items():
            points = np.concatenate(
                ret_dict["points_{}_0".format(lidar_id)]
                + ret_dict["points_{}_1".format(lidar_id)],
                axis=0,
            )
            intensity = np.concatenate(
                ret_dict["intensity_{}_0".format(lidar_id)]
                + ret_dict["intensity_{}_1".format(lidar_id)],
                axis=0,
            )

            if NORMALIZE_INTENSITY:
                intensity = np.tanh(intensity)

            # concatenate x, y, z and intensity
            points = np.column_stack((points, intensity))
            concat_points.append(points)

            write_points_to_bag(points, lidar_name)

        # write_points_to_bag(np.concatenate(concat_points, axis=0), "concatenated")


def to_transform(from_frame_id, to_frame_id, stamp, trans_mat):
    t = tf.transformations.translation_from_matrix(trans_mat)
    q = tf.transformations.quaternion_from_matrix(trans_mat)
    tf_msg = TransformStamped()
    tf_msg.header.stamp = stamp
    tf_msg.header.frame_id = from_frame_id
    tf_msg.child_frame_id = to_frame_id
    tf_msg.transform.translation.x = t[0]
    tf_msg.transform.translation.y = t[1]
    tf_msg.transform.translation.z = t[2]
    tf_msg.transform.rotation.x = q[0]
    tf_msg.transform.rotation.y = q[1]
    tf_msg.transform.rotation.z = q[2]
    tf_msg.transform.rotation.w = q[3]
    return tf_msg


def convert_range_image_to_point_cloud(
    frame, range_images, camera_projections, range_image_top_pose, ri_indexes=(0, 1)
):
    """Convert range images to point cloud. modified from
    https://github.com/waymo-research/waymo-open-dataset/blob/master/waymo_open_dataset/utils/range_image_utils.py#L612

    Args:
      frame: open dataset frame
       range_images: A dict of {laser_name, [range_image_first_return,
         range_image_second_return]}.
       camera_projections: A dict of {laser_name,
         [camera_projection_from_first_return,
         camera_projection_from_second_return]}.
      range_image_top_pose: range image pixel pose for top lidar.
      ri_indexes: 0 for the first return, 1 for the second return.
    Returns:
      points: {[N, 3]} list of 3d lidar points of length 5 (number of lidars).
      cp_points: {[N, 6]} list of camera projections of length 5
        (number of lidars).
    """
    tf = tensorflow
    calibrations = sorted(frame.context.laser_calibrations, key=lambda c: c.name)
    ret_dict = defaultdict(list)

    frame_pose = tf.convert_to_tensor(value=np.reshape(np.array(frame.pose.transform), [4, 4]))
    # [H, W, 6]
    range_image_top_pose_tensor = tf.reshape(
        tf.convert_to_tensor(value=range_image_top_pose.data), range_image_top_pose.shape.dims
    )
    # [H, W, 3, 3]
    range_image_top_pose_tensor_rotation = transform_utils.get_rotation_matrix(
        range_image_top_pose_tensor[..., 0],
        range_image_top_pose_tensor[..., 1],
        range_image_top_pose_tensor[..., 2],
    )
    range_image_top_pose_tensor_translation = range_image_top_pose_tensor[..., 3:]
    range_image_top_pose_tensor = transform_utils.get_transform(
        range_image_top_pose_tensor_rotation, range_image_top_pose_tensor_translation
    )

    for c in calibrations:
        for ri_index in ri_indexes:
            range_image = range_images[c.name][ri_index]
            if len(c.beam_inclinations) == 0:
                beam_inclinations = range_image_utils.compute_inclination(
                    tf.constant([c.beam_inclination_min, c.beam_inclination_max]),
                    height=range_image.shape.dims[0],
                )
            else:
                beam_inclinations = tf.constant(c.beam_inclinations)

            beam_inclinations = tf.reverse(beam_inclinations, axis=[-1])
            extrinsic = np.reshape(np.array(c.extrinsic.transform), [4, 4])

            range_image_tensor = tf.reshape(
                tf.convert_to_tensor(value=range_image.data), range_image.shape.dims
            )
            pixel_pose_local = None
            frame_pose_local = None
            if c.name == dataset_pb2.LaserName.TOP:
                pixel_pose_local = range_image_top_pose_tensor
                pixel_pose_local = tf.expand_dims(pixel_pose_local, axis=0)
                frame_pose_local = tf.expand_dims(frame_pose, axis=0)
            range_image_mask = range_image_tensor[..., 0] > 0

            # No Label Zone
            if FILTER_NO_LABEL_ZONE_POINTS:
                nlz_mask = range_image_tensor[..., 3] != 1.0  # 1.0: in NLZ
                range_image_mask = range_image_mask & nlz_mask

            range_image_cartesian = range_image_utils.extract_point_cloud_from_range_image(
                tf.expand_dims(range_image_tensor[..., 0], axis=0),
                tf.expand_dims(extrinsic, axis=0),
                tf.expand_dims(tf.convert_to_tensor(value=beam_inclinations), axis=0),
                pixel_pose=pixel_pose_local,
                frame_pose=frame_pose_local,
            )

            range_image_cartesian = tf.squeeze(range_image_cartesian, axis=0)
            points_tensor = tf.gather_nd(
                range_image_cartesian, tf.compat.v1.where(range_image_mask)
            )

            ret_dict["points_{}_{}".format(c.name, ri_index)].append(points_tensor.numpy())

            # Note: channel 1 is intensity
            # https://github.com/waymo-research/waymo-open-dataset/blob/master/waymo_open_dataset/dataset.proto#L176
            intensity_tensor = tf.gather_nd(range_image_tensor[..., 1], tf.where(range_image_mask))
            ret_dict["intensity_{}_{}".format(c.name, ri_index)].append(intensity_tensor.numpy())

    return ret_dict


def waymo2bag():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--load_dir",
        default="/data/tfrecord",
        help="directory to load Waymo Open Dataset tfrecords",
    )
    parser.add_argument(
        "--save_dir",
        default="/data/rosbag",
        help="directory to save converted rosbag1 data",
    )
    args = parser.parse_args()

    converter = Waymo2Bag(args.load_dir, args.save_dir)
    converter.convert()


if __name__ == '__main__':
    waymo2bag()
