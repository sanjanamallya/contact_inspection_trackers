#!/usr/bin/python

import cv2
import sys
from cv_bridge import CvBridge
import rospy
import numpy as np
import message_filters
import argparse
# import open3d as o3d
from sensor_msgs.msg import Image, TimeReference
from vision_msgs.msg import BoundingBox2D
from vision_msgs.msg import Detection2D
from std_msgs.msg import Float32, Int8


"""



Purpose of this ROS Node is to track objects with a CSRT tracker but without depth information and therefore without 
depth-reliant scaling. bbox size-dependent scaling functionality remains. [This ROS Node is adapted from Lorenz Stangier's tracker_scale.py ("implements a object tracker that used a depth image
to resize it's generated bounding box")]]

It takes the following parameters:
    ~color_img_topic:
        The name of the topic this node should subscribe to for receiving the color img
    ~publish_tracked:
        Either True or False. If True, an Image where the BBox is lying over the RGB image is being published
    ~oob_threshold:
        A integer value that specifies the distance in pixels that the bounding box has to be away from any image
        border to be invalid

It subscribes to the following topics:
    /perception/tracker/bboxIn (vision_msgs/BoundingBox2D):
        The inital bounding box of to object to track.
        Only the first message on this topic will be used.

    There is another subscriber that depends on the topics ~color_img_topic.

It publishes on the following topics:
    /perception/tracker/bboxOut (vision_msgs/BoundingBox2D):
        The current bounding box of the object that's being tracked.
    /perception/tracker/bboxImage (sensor_msgs/Image)
        The current RGB Image with overlayed BBox (only if enabled)
    /perception/tracker/status (Int8)
        The status of the current bounding box (1 = Valid, 0 = Invalid)

"""

class csrt_tracker(object):

    #
    # Initilization Functions
    #

    def __init__(self,  tracker="1",debug=False):
        self.tracker = tracker
        self.init_variables_hard(debug)
        self.init_subscribers()
        self.init_publisher()



    # for node startup
    def init_variables_hard(self, debug):
        # print(self.tracker)
        if not debug:
            self.bbox_in_topic = rospy.get_param(
                "~bbox_in_topic", "/perception/roi/rect"
            )
            self.color_image_topic = rospy.get_param(
                "~color_img_topic", "/front_depth_camera/color/image_raw"
            )
            self.depth_image_topic = rospy.get_param(
                "~depth_img_topic", "/front_depth_camera/aligned_depth_to_color/image_raw"

            )
            self.publish_result_img = rospy.get_param(
                "~publish_tracked", "False"
            )

            self.publish_pose = rospy.get_param(
                "~publish_pose", "False"
            )
            self.min_depth = rospy.get_param("~min_depth", 0.1)
            self.max_depth = rospy.get_param("~max_depth", 20.0)
            self.oob_threshold = rospy.get_param("~oob_threshold", 10)
            self.max_bbox_ratio = rospy.get_param("~max_bbox_ratio", 1.0)
        else:
            self.color_image_topic = "/front_track_camera/fisheye1/camera_raw"
            self.bbox_in_topic = "/perception/roi/rect"
            self.publish_result_img = True
            self.oob_threshold = 10
            self.max_bbox_ratio = 1.0
            self.max_depth = 5.0
            self.min_depth = 0.5

        self._bridge = CvBridge()

        self._color_timestamp = -1

        self._current_color_msg = None

        self._inital_bbox = None
        self._current_bbox = None
        self.savepointcloud = True
        self._original_distance = -1
        self._current_distance = -1
        self._previous_distance = -1
        OPENCV_OBJECT_TRACKERS = {
            "1": cv2.TrackerCSRT_create,
            "2": cv2.TrackerKCF_create,
            "3": cv2.TrackerBoosting_create,
            "4": cv2.TrackerMIL_create,
            "5": cv2.TrackerTLD_create,
            "6": cv2.TrackerMedianFlow_create,
            "7": cv2.TrackerMOSSE_create
	        }
        print("TRAKER CURRENTLY BEING UTILIZED", OPENCV_OBJECT_TRACKERS[self.tracker])
        self._tracker = OPENCV_OBJECT_TRACKERS[self.tracker]()
        

        self._is_first_frame = True

        self._has_scale_changed = False
        self._scale = 1.0
        self._fallback_scale = 0.4
        # self._max_ratio = 1.0

        self._last_bbox = None

        self._current_status = 1
        self.focal_length = 619.2664184570312
        self.cx = 324
        self.cy = 246
        self.height = 480
        self.width = 640

        self.Q2 = np.float32([[1,0,0,0],
        [0,-1,0,0],
        [0,0,self.focal_length*0.018,0], #Focal length multiplication obtained experimentally. 
        [0,0,0,1]])


    # for easy tracker re-initialization
    def init_variables_soft(self):

        self._color_timestamp = -1

        self._current_color_msg = None

        self._inital_bbox = None
        self._current_bbox = None

        self._original_distance = -1
        self._current_distance = -1
        self._previous_distance = -1

        self._is_first_frame = True

        self._has_scale_changed = False
        self._scale = 1.0
        self._fallback_scale = 0.4
        # self._max_ratio = 1.0

        self._last_bbox = None

        self._current_status = 1

    def init_subscribers(self):
        #0.033
        # sub_image = rospy.Subscriber(self.color_image_topic, Image, self.got_image_color)
        sub_color = message_filters.Subscriber(self.color_image_topic, Image)
        sub_depth = message_filters.Subscriber(self.depth_image_topic, Image)
        sync = message_filters.ApproximateTimeSynchronizer([sub_color, sub_depth], 2, 0.1)
        sync.registerCallback(self.got_image)
        # print("registered")
        sub_bbox = rospy.Subscriber(
            self.bbox_in_topic, BoundingBox2D, self.got_bounding_box
        )
    def init_publisher(self):
        self._pub_bbox = rospy.Publisher(
            "/perception/tracker/bboxOut", Detection2D, queue_size=30
        )

        self._pub_result_img = rospy.Publisher(
            "/perception/tracker/bboxImage", Image, queue_size=30
        )

        self._pub_status = rospy.Publisher(
            "/perception/tracker/status", TimeReference, queue_size=30

        )


    #
    # Helper Functions
    #

    def check_point_oob(self, point, image, threshold):
        if point[0] < threshold or point[0] > image.shape[1] - threshold:
            return True

        if point[1] < threshold or point[1] > image.shape[0] - threshold:
            return True

        return False

    def calculate_bbox_center(self, bbox):
        center = (bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2)
        center = tuple([int(x) for x in center])

        return center

    def scale_bbox(self, bbox, factor):
        width, height = bbox[2], bbox[3]

        width = width * factor
        height = height * factor

        center = self.calculate_bbox_center(bbox)

        new_bbox = (
            center[0] - width / 2,
            center[1] - height / 2,
            width,
            height,
        )

        return tuple([int(x) for x in new_bbox])

    def get_bbox_scale(self, inital_bbox, new_bbox):
        i_width, i_height = inital_bbox[2], inital_bbox[3]
        n_width, n_height = new_bbox[2], new_bbox[3]

        width_scale = n_width / i_width
        height_scale = n_height / i_height

        return (width_scale + height_scale) / 2

    def spin(self):
        rate = rospy.Rate(30)

        while not rospy.is_shutdown():
            rate.sleep()

    #
    # Callback functions for ROS Subscriptions
    #

    def got_bounding_box(self, boundingBox):
        self.init_variables_hard(False)

        center = (boundingBox.center.x, boundingBox.center.y)
        width = boundingBox.size_x
        height = boundingBox.size_y

        if self._inital_bbox is None:
            rospy.loginfo("BBox Received")
            self._inital_bbox = (
                int(center[0] - width / 2),
                int(center[1] - height / 2),
                width,
                height,
            )

    def write_pointcloud(self, vertices, colors, filename):
        colors = colors.reshape(-1,3)
        vertices = np.hstack([vertices.reshape(-1,3),colors])
        

        ply_header = '''ply
            format ascii 1.0
            element vertex %(vert_num)d
            property float x
            property float y
            property float z
            property uchar red
            property uchar green
            property uchar blue
            end_header
            '''
        
        with open(filename, 'w') as f:
            f.write(ply_header %dict(vert_num=len(vertices)))
            np.savetxt(f,vertices,'%f %f %f %d %d %d')

    def got_image(self, rgb_msg, depth_msg):

    
        color_image = self._bridge.imgmsg_to_cv2(rgb_msg, '8UC3')
        depth_image = self._bridge.imgmsg_to_cv2(
            depth_msg, "8UC1"
        )

        ## Enable to reporject depth map th 3D
        """
        points_3D = cv2.reprojectImageTo3D(depth_image, self.Q2)
        mask_map = depth_image > 0
        output_points = points_3D[mask_map]
        output_colors = color_image[mask_map]
        cv2.imwrite("color.jpg", color_image)
        cv2.imwrite("depth.jpg", depth_image)

        if self.savepointcloud:
            # output_file = "reconstructed.ply"
            print ("\n Creating the output file... \n")
            self.write_pointcloud(output_points, output_colors, output_file)
            self.savepointcloud = False
        """

        final_bbox = None

        if self._is_first_frame and self._inital_bbox is not None:
            rospy.loginfo("Initializing tracker")
            current_bbox = self._inital_bbox
            bbox_center = self.calculate_bbox_center(current_bbox)
            self._tracker.init(color_image, current_bbox)
            self._is_first_frame = False
            final_bbox = current_bbox   

        elif not self._is_first_frame:

            ok, self.tracker_suggested_bbox = self._tracker.update(
                    color_image
                )

            if ok:
                final_bbox = self.tracker_suggested_bbox

            else:
                self._current_status = 0
                T = TimeReference()
                T.header.stamp = depth_msg.header.stamp
                T.source = str(self._current_status)
                self._pub_status.publish(T)


        if final_bbox is not None:
            self._last_bbox = final_bbox

            width_ratio = float(final_bbox[2]) / float(color_image.shape[1])
            height_ratio = float(final_bbox[3]) / float(color_image.shape[0])

            if (
                width_ratio > self.max_bbox_ratio or height_ratio > self.max_bbox_ratio
            ) and self._scale != self._fallback_scale:
                rospy.loginfo("Scaling down...")

                self._scale = self._fallback_scale
                self._has_scale_changed = True
            elif (
                width_ratio < self.max_bbox_ratio and height_ratio < self.max_bbox_ratio
            ) and self._scale == self._fallback_scale:
                rospy.loginfo("Scaling back up...")

                self._scale = 1.0
                self._has_scale_changed = True

            center = self.calculate_bbox_center(final_bbox)

            if self.check_point_oob(center, color_image, self.oob_threshold):
                self._current_status = 0

            bbox_message = Detection2D()

            # Initialize header info with that of depthmap's
            bbox_message.header.stamp = depth_msg.header.stamp
            bbox_message.header.seq = "bbox_INFO"
            
            # bbox info 
            bbox_message.bbox.size_x = final_bbox[2]
            bbox_message.bbox.size_y = final_bbox[3]

            bbox_message.bbox.center.theta = 0
            bbox_message.bbox.center.x = final_bbox[0] + final_bbox[2] / 2
            bbox_message.bbox.center.y = final_bbox[1] + final_bbox[3] / 2

            self._pub_bbox.publish(bbox_message)

            T = TimeReference()
            T.header.stamp = depth_msg.header.stamp
            T.source = str(self._current_status)
            self._pub_status.publish(T)

            # cv2.imshow('depth',depth_image)
            # cv2.waitKey()

            if self.publish_result_img:
                final_bbox = tuple([int(i) for i in final_bbox])

                if self._current_status == 1:
                    cv2.rectangle(color_image, (final_bbox[0], final_bbox[1]), (final_bbox[0]+final_bbox[2], final_bbox[1]+final_bbox[3]), (0, 0, 255), 2)
                else:
                    cv2.rectangle(color_image, (final_bbox[0], final_bbox[1]), (final_bbox[0]+final_bbox[2], final_bbox[1]+final_bbox[3]), (255, 0, 0), 2)

                cv2.circle(color_image, center, 3, (255, 0, 0), 2)

                imgmsg = self._bridge.cv2_to_imgmsg(
                    color_image, 'rgb8'
                )
  
                self._pub_result_img.publish(imgmsg)
        
    # def got_image_color(self, rgb_msg):

    #         # print("got_image_color")
    #         color_image = self._bridge.imgmsg_to_cv2(rgb_msg, '8UC3')

    #         final_bbox = None

    #         if self._is_first_frame and self._inital_bbox is not None:
    #             rospy.loginfo("Initializing tracker")
    #             current_bbox = self._inital_bbox
    #             bbox_center = self.calculate_bbox_center(current_bbox)
    #             self._tracker.init(color_image, current_bbox)
    #             self._is_first_frame = False
    #             final_bbox = current_bbox   

    #         elif not self._is_first_frame:

    #             ok, self.tracker_suggested_bbox = self._tracker.update(
    #                     color_image
    #                 )

    #             if ok:
    #                 final_bbox = self.tracker_suggested_bbox

    #             else:
    #                 self._current_status = 0
    #                 status_message = Int8()
    #                 status_message.data = self._current_status
    #                 self._pub_status.publish(status_message)


    #         if final_bbox is not None:
    #             self._last_bbox = final_bbox

    #             width_ratio = float(final_bbox[2]) / float(color_image.shape[1])
    #             height_ratio = float(final_bbox[3]) / float(color_image.shape[0])

    #             if (
    #                 width_ratio > self.max_bbox_ratio or height_ratio > self.max_bbox_ratio
    #             ) and self._scale != self._fallback_scale:
    #                 rospy.loginfo("Scaling down...")

    #                 self._scale = self._fallback_scale
    #                 self._has_scale_changed = True
    #             elif (
    #                 width_ratio < self.max_bbox_ratio and height_ratio < self.max_bbox_ratio
    #             ) and self._scale == self._fallback_scale:
    #                 rospy.loginfo("Scaling back up...")

    #                 self._scale = 1.0
    #                 self._has_scale_changed = True

    #             center = self.calculate_bbox_center(final_bbox)

    #             if self.check_point_oob(center, color_image, self.oob_threshold):
    #                 self._current_status = 0

    #             bbox_message = BoundingBox2D()

    #             bbox_message.size_x = final_bbox[2]
    #             bbox_message.size_y = final_bbox[3]

    #             bbox_message.center.theta = 0
    #             bbox_message.center.x = final_bbox[0] + final_bbox[2] / 2
    #             bbox_message.center.y = final_bbox[1] + final_bbox[3] / 2

    #             self._pub_bbox.publish(bbox_message)

    #             status_message = Int8()
    #             status_message.data = self._current_status
    #             self._pub_status.publish(status_message)


    #             if self.publish_result_img:
    #                 final_bbox = tuple([int(i) for i in final_bbox])

    #                 if self._current_status == 1:
    #                     cv2.rectangle(color_image, (final_bbox[0], final_bbox[1]), (final_bbox[0]+final_bbox[2], final_bbox[1]+final_bbox[3]), (0, 0, 255), 2)
    #                 else:
    #                     cv2.rectangle(color_image, (final_bbox[0], final_bbox[1]), (final_bbox[0]+final_bbox[2], final_bbox[1]+final_bbox[3]), (255, 0, 0), 2)

    #                 cv2.circle(color_image, center, 3, (255, 0, 0), 2)

    #                 # print("Publishing tracked bbox")
    #                 imgmsg = self._bridge.cv2_to_imgmsg(
    #                     color_image, 'rgb8'
    #                 )
    
    #                 self._pub_result_img.publish(imgmsg)

if __name__ == "__main__":
    rospy.init_node("csrt_tracker")
    rospy.loginfo("Starting csrt tracker...")
    myargv = rospy.myargv(argv=sys.argv)

    # When started from roslaunch, argc is > 1
    if len(sys.argv) > 1:
        debug = False
    else:  # When started from VSCode, argc == 1
        debug = True

    st = csrt_tracker(myargv[1], debug=debug)
    rospy.loginfo("Running csrt tracker in debug mode: {}".format(debug))

    st.spin()
