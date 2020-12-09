#!/usr/bin/env python


import copy
import matplotlib
matplotlib.use("Agg")  # NOQA
import matplotlib.pyplot as plt
import numpy as np
import os
import re
import sys
import threading

# OpenCV import for python3.5
sys.path.remove('/opt/ros/{}/lib/python2.7/dist-packages'.format(os.getenv('ROS_DISTRO')))  # NOQA
import cv2  # NOQA
sys.path.append('/opt/ros/{}/lib/python2.7/dist-packages'.format(os.getenv('ROS_DISTRO')))  # NOQA

from chainercv.visualizations import vis_bbox
from cv_bridge import CvBridge
from edgetpu.detection.engine import DetectionEngine
import PIL.Image
import rospkg
import rospy

from dynamic_reconfigure.server import Server
from jsk_recognition_msgs.msg import ClassificationResult
from jsk_recognition_msgs.msg import Rect
from jsk_recognition_msgs.msg import RectArray
from jsk_topic_tools import ConnectionBasedTransport
from sensor_msgs.msg import Image, CompressedImage

from coral_usb.cfg import EdgeTPUObjectDetectorConfig


class EdgeTPUObjectDetector(ConnectionBasedTransport):

    def __init__(self):
        # get image_trasport before ConnectionBasedTransport subscribes ~input
        self.transport_hint = rospy.get_param('~image_transport', 'raw')
        rospy.loginfo("Using transport {}".format(self.transport_hint))
        #
        super(EdgeTPUObjectDetector, self).__init__()
        rospack = rospkg.RosPack()
        pkg_path = rospack.get_path('coral_usb')
        self.bridge = CvBridge()
        self.classifier_name = rospy.get_param(
            '~classifier_name', rospy.get_name())
        model_file = os.path.join(
            pkg_path,
            './models/mobilenet_ssd_v2_coco_quant_postprocess_edgetpu.tflite')
        model_file = rospy.get_param('~model_file', model_file)
        label_file = rospy.get_param(
            '~label_file', os.path.join(pkg_path, './models/coco_labels.txt'))
        duration = rospy.get_param('~visualize_duration', 0.1)
        self.enable_visualization = rospy.get_param(
            '~enable_visualization', True)

        self.engine = DetectionEngine(model_file)
        self.label_ids, self.label_names = self._load_labels(label_file)

        # dynamic reconfigure
        self.srv = Server(EdgeTPUObjectDetectorConfig, self.config_callback)

        self.pub_rects = self.advertise(
            '~output/rects', RectArray, queue_size=1)
        self.pub_class = self.advertise(
            '~output/class', ClassificationResult, queue_size=1)

        # visualize timer
        if self.enable_visualization:
            self.lock = threading.Lock()
            self.pub_image = self.advertise(
                '~output/image', Image, queue_size=1)
            self.pub_image_compressed = self.advertise(
                '~output/image/compressed', CompressedImage, queue_size=1)
            self.timer = rospy.Timer(
                rospy.Duration(duration), self.visualize_cb)
            self.img = None
            self.header = None
            self.bboxes = None
            self.labels = None
            self.scores = None

    def subscribe(self):
        if self.transport_hint == 'compressed':
            self.sub_image = rospy.Subscriber(
                '{}/compressed'.format(rospy.resolve_name('~input')),
                CompressedImage, self.image_cb, queue_size=1, buff_size=2**26)
        else:
            self.sub_image = rospy.Subscriber(
                '~input', Image, self.image_cb, queue_size=1, buff_size=2**26)

    def unsubscribe(self):
        self.sub_image.unregister()

    @property
    def visualize(self):
        return self.pub_image.get_num_connections() > 0 or \
            self.pub_image_compressed.get_num_connections() > 0

    def config_callback(self, config, level):
        self.score_thresh = config.score_thresh
        self.top_k = config.top_k
        return config

    def _load_labels(self, path):
        p = re.compile(r'\s*(\d+)(.+)')
        with open(path, 'r', encoding='utf-8') as f:
            lines = (p.match(line).groups() for line in f.readlines())
            labels = {int(num): text.strip() for num, text in lines}
            return list(labels.keys()), list(labels.values())

    def image_cb(self, msg):
        if self.transport_hint == 'compressed':
            np_arr = np.fromstring(msg.data, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        else:
            img = self.bridge.imgmsg_to_cv2(msg, desired_encoding='rgb8')
        H, W = img.shape[:2]
        objs = self.engine.DetectWithImage(
            PIL.Image.fromarray(img), threshold=self.score_thresh,
            keep_aspect_ratio=True, relative_coord=True,
            top_k=self.top_k)

        bboxes = []
        scores = []
        labels = []
        rect_msg = RectArray(header=msg.header)
        for obj in objs:
            x_min, y_min, x_max, y_max = obj.bounding_box.flatten().tolist()
            x_min = int(np.round(x_min * W))
            y_min = int(np.round(y_min * H))
            x_max = int(np.round(x_max * W))
            y_max = int(np.round(y_max * H))
            bboxes.append([y_min, x_min, y_max, x_max])
            scores.append(obj.score)
            labels.append(self.label_ids.index(int(obj.label_id)))
            rect = Rect(
                x=x_min, y=y_min,
                width=x_max - x_min, height=y_max - y_min)
            rect_msg.rects.append(rect)
        bboxes = np.array(bboxes)
        scores = np.array(scores)
        labels = np.array(labels)

        cls_msg = ClassificationResult(
            header=msg.header,
            classifier=self.classifier_name,
            target_names=self.label_names,
            labels=labels,
            label_names=[self.label_names[lbl] for lbl in labels],
            label_proba=scores)

        self.pub_rects.publish(rect_msg)
        self.pub_class.publish(cls_msg)

        if self.enable_visualization:
            with self.lock:
                self.img = img
                self.header = msg.header
                self.bboxes = bboxes
                self.labels = labels
                self.scores = scores

    def visualize_cb(self, event):
        if (not self.visualize or self.img is None
                or self.header is None or self.bboxes is None
                or self.labels is None or self.scores is None):
            return

        with self.lock:
            img = self.img.copy()
            header = copy.deepcopy(self.header)
            bboxes = self.bboxes.copy()
            labels = self.labels.copy()
            scores = self.scores.copy()

        fig = plt.figure(
            tight_layout={'pad': 0})
        ax = plt.Axes(fig, [0., 0., 1., 1.])
        ax.axis('off')
        fig.add_axes(ax)
        vis_bbox(
            img.transpose((2, 0, 1)),
            bboxes, labels, scores,
            label_names=self.label_names, ax=ax)
        fig.canvas.draw()
        w, h = fig.canvas.get_width_height()
        vis_img = np.fromstring(
            fig.canvas.tostring_rgb(), dtype=np.uint8)
        vis_img.shape = (h, w, 3)
        fig.clf()
        plt.close()
        if self.pub_image.get_num_connections() > 0:
            vis_msg = self.bridge.cv2_to_imgmsg(vis_img, 'rgb8')
            # BUG: https://answers.ros.org/question/316362/sensor_msgsimage-generates-float-instead-of-int-with-python3/  # NOQA
            vis_msg.step = int(vis_msg.step)
            vis_msg.header = header
            self.pub_image.publish(vis_msg)
        if self.pub_image_compressed.get_num_connections() > 0:
            # publish compressed http://wiki.ros.org/rospy_tutorials/Tutorials/WritingImagePublisherSubscriber  # NOQA
            vis_compressed_msg = CompressedImage()
            vis_compressed_msg.header = header
            vis_compressed_msg.format = "jpeg"
            vis_img_rgb = cv2.cvtColor(vis_img, cv2.COLOR_BGR2RGB)
            vis_compressed_msg.data = np.array(
                cv2.imencode('.jpg', vis_img_rgb)[1]).tostring()
            self.pub_image_compressed.publish(vis_compressed_msg)


if __name__ == '__main__':
    rospy.init_node('edgetpu_object_detector')
    detector = EdgeTPUObjectDetector()
    rospy.spin()
