# Ultralytics YOLO 🚀, AGPL-3.0 license
"""
Ultralytics Results, Boxes and Masks classes for handling inference results.

Usage: See https://docs.ultralytics.com/modes/predict/
"""

from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import numpy as np
import torch
import cv2

from ultralytics.data.augment import LetterBox
from ultralytics.utils import LOGGER, SimpleClass, ops
from ultralytics.utils.plotting import Annotator, colors, save_one_box, cls_to_color
from ultralytics.utils.torch_utils import smart_inference_mode

HISTORY_VLA_pt = None
class BaseTensor(SimpleClass):
    """Base tensor class with additional methods for easy manipulation and device handling."""

    def __init__(self, data, orig_shape) -> None:
        """
        Initialize BaseTensor with data and original shape.

        Args:
            data (torch.Tensor | np.ndarray): Predictions, such as bboxes, masks and keypoints.
            orig_shape (tuple): Original shape of image.
        """
        assert isinstance(data, (torch.Tensor, np.ndarray))
        self.data = data
        self.orig_shape = orig_shape

    @property
    def shape(self):
        """Return the shape of the data tensor."""
        return self.data.shape

    def cpu(self):
        """Return a copy of the tensor on CPU memory."""
        return self if isinstance(self.data, np.ndarray) else self.__class__(self.data.cpu(), self.orig_shape)

    def numpy(self):
        """Return a copy of the tensor as a numpy array."""
        return self if isinstance(self.data, np.ndarray) else self.__class__(self.data.numpy(), self.orig_shape)

    def cuda(self):
        """Return a copy of the tensor on GPU memory."""
        return self.__class__(torch.as_tensor(self.data).cuda(), self.orig_shape)

    def to(self, *args, **kwargs):
        """Return a copy of the tensor with the specified device and dtype."""
        return self.__class__(torch.as_tensor(self.data).to(*args, **kwargs), self.orig_shape)

    def __len__(self):  # override len(results)
        """Return the length of the data tensor."""
        return len(self.data)

    def __getitem__(self, idx):
        """Return a BaseTensor with the specified index of the data tensor."""
        return self.__class__(self.data[idx], self.orig_shape)


class Results(SimpleClass):
    """
    A class for storing and manipulating inference results.

    Args:
        orig_img (numpy.ndarray): The original image as a numpy array.
        path (str): The path to the image file.
        names (dict): A dictionary of class names.
        boxes (torch.tensor, optional): A 2D tensor of bounding box coordinates for each detection.
        masks (torch.tensor, optional): A 3D tensor of detection masks, where each mask is a binary image.
        probs (torch.tensor, optional): A 1D tensor of probabilities of each class for classification task.
        keypoints (List[List[float]], optional): A list of detected keypoints for each object.
        drive_map (torch.Tensor, optional): The drivable area segmentation prediction output.
        lane_map (torch.Tensor, optional): The lane line segmentation prediction output


    Attributes:
        orig_img (numpy.ndarray): The original image as a numpy array.
        orig_shape (tuple): The original image shape in (height, width) format.
        boxes (Boxes, optional): A Boxes object containing the detection bounding boxes.
        masks (Masks, optional): A Masks object containing the detection masks.
        probs (Probs, optional): A Probs object containing probabilities of each class for classification task.
        keypoints (Keypoints, optional): A Keypoints object containing detected keypoints for each object.
        speed (dict): A dictionary of preprocess, inference, and postprocess speeds in milliseconds per image.
        names (dict): A dictionary of class names.
        drive_map (numpy.ndarray, optional): The drivable area segmentation prediction output.
        drive_map (numpy.ndarray, optional): The lane line segmentation prediction output
        path (str): The path to the image file.
        keypoints (Keypoints, optional): A Keypoints object containing detected keypoints for each object.
        speed (dict): A dictionary of preprocess, inference and postprocess speeds in milliseconds per image.
        _keys (tuple): A tuple of attribute names for non-empty attributes.
    """

    def __init__(self,
                 orig_img,
                 path, names,
                 boxes=None,
                 masks=None,
                 probs=None,
                 keypoints=None,
                 drive_map=None,
                 lane_map=None,
                 seg_map=None) -> None:
        """Initialize the Results class."""
        self.orig_img = orig_img
        self.drive_map = drive_map
        self.lane_map = lane_map
        self.seg_map = seg_map
        self.orig_shape = orig_img.shape[:2]
        self.boxes = Boxes(boxes, self.orig_shape) if boxes is not None else None  # native size boxes
        self.masks = Masks(masks, self.orig_shape) if masks is not None else None  # native size or imgsz masks
        self.probs = Probs(probs) if probs is not None else None
        self.keypoints = Keypoints(keypoints, self.orig_shape) if keypoints is not None else None
        self.speed = {'preprocess': None, 'inference': None, 'postprocess': None}  # milliseconds per image
        self.names = names  # names of OD task
        self.path = path
        self.save_dir = None
        self._keys = ('boxes', 'masks', 'probs', 'keypoints', 'drive_map', 'lane_map') # update to include drive_map, lane_map 

    def __getitem__(self, idx):
        """Return a Results object for the specified index."""
        return self._apply('__getitem__', idx)

    def __len__(self):
        """Return the number of detections in the Results object."""
        for k in self._keys:
            v = getattr(self, k)
            if v is not None:
                return len(v)

    def update(self, boxes=None, masks=None, probs=None):
        """Update the boxes, masks, and probs attributes of the Results object."""
        if boxes is not None:
            ops.clip_boxes(boxes, self.orig_shape)  # clip boxes
            self.boxes = Boxes(boxes, self.orig_shape)
        if masks is not None:
            self.masks = Masks(masks, self.orig_shape)
        if probs is not None:
            self.probs = probs

    def _apply(self, fn, *args, **kwargs):
        """
        Applies a function to all non-empty attributes and returns a new Results object with modified attributes. This
        function is internally called by methods like .to(), .cuda(), .cpu(), etc.

        Args:
            fn (str): The name of the function to apply.
            *args: Variable length argument list to pass to the function.
            **kwargs: Arbitrary keyword arguments to pass to the function.

        Returns:
            Results: A new Results object with attributes modified by the applied function.
        """
        r = self.new()
        for k in self._keys:
            v = getattr(self, k)
            if v is not None:
                setattr(r, k, getattr(v, fn)(*args, **kwargs))
        return r

    def cpu(self):
        """Return a copy of the Results object with all tensors on CPU memory."""
        return self._apply('cpu')

    def numpy(self):
        """Return a copy of the Results object with all tensors as numpy arrays."""
        return self._apply('numpy')

    def cuda(self):
        """Return a copy of the Results object with all tensors on GPU memory."""
        return self._apply('cuda')

    def to(self, *args, **kwargs):
        """Return a copy of the Results object with tensors on the specified device and dtype."""
        return self._apply('to', *args, **kwargs)

    def new(self):
        """Return a new Results object with the same image, path, and names."""
        return Results(orig_img=self.orig_img, path=self.path, names=self.names, drive_map=self.drive_map, lane_map=self.lane_map, seg_map=self.seg_map)

    def plot(
        self,
        conf=True,
        line_width=None,
        font_size=None,
        font='Arial.ttf',
        pil=False,
        img=None,
        im_gpu=None,
        kpt_radius=5,
        kpt_line=True,
        labels=True,
        boxes=True,
        masks=True,
        probs=True,
    ):
        """
        Plots the detection results on an input RGB image. Accepts a numpy array (cv2) or a PIL Image.

        Args:
            conf (bool): Whether to plot the detection confidence score.
            line_width (float, optional): The line width of the bounding boxes. If None, it is scaled to the image size.
            font_size (float, optional): The font size of the text. If None, it is scaled to the image size.
            font (str): The font to use for the text.
            pil (bool): Whether to return the image as a PIL Image.
            img (numpy.ndarray): Plot to another image. if not, plot to original image.
            im_gpu (torch.Tensor): Normalized image in gpu with shape (1, 3, 640, 640), for faster mask plotting.
            kpt_radius (int, optional): Radius of the drawn keypoints. Default is 5.
            kpt_line (bool): Whether to draw lines connecting keypoints.
            labels (bool): Whether to plot the label of bounding boxes.
            boxes (bool): Whether to plot the bounding boxes.
            masks (bool): Whether to plot the masks.
            probs (bool): Whether to plot classification probability

        Returns:
            (numpy.ndarray): A numpy array of the annotated image.

        Example:
            ```python
            from PIL import Image
            from ultralytics import YOLO

            model = YOLO('yolov8n.pt')
            results = model('bus.jpg')  # results list
            for r in results:
                im_array = r.plot()  # plot a BGR numpy array of predictions
                im = Image.fromarray(im_array[..., ::-1])  # RGB PIL image
                im.show()  # show image
                im.save('results.jpg')  # save image
            ```
        """
        if img is None and isinstance(self.orig_img, torch.Tensor):
            img = (self.orig_img[0].detach().permute(1, 2, 0).contiguous() * 255).to(torch.uint8).cpu().numpy()

        names = self.names
        pred_boxes, show_boxes = self.boxes, boxes
        pred_masks, show_masks = self.masks, masks
        pred_probs, show_probs = self.probs, probs
        annotator = Annotator(
            deepcopy(self.orig_img if img is None else img),
            line_width,
            font_size,
            font,
            pil or (pred_probs is not None and show_probs),  # Classify tasks default to pil=True
            example=names)

        # Plot Segment results
        if pred_masks and show_masks:
            if im_gpu is None:
                img = LetterBox(pred_masks.shape[1:])(image=annotator.result())
                im_gpu = torch.as_tensor(img, dtype=torch.float16, device=pred_masks.data.device).permute(
                    2, 0, 1).flip(0).contiguous() / 255
            idx = pred_boxes.cls if pred_boxes else range(len(pred_masks))
            annotator.masks(pred_masks.data, colors=[colors(x, True) for x in idx], im_gpu=im_gpu)

        Final_VLA_pt = (9999,9999,9999,9999)
        Final_DCA_pt = (9999,9999,9999,9999)
        Final_VPA_pt = (9999,9999,9999,9999)
        Final_DUA_d_pt = (9999,9999,9999,9999)
        Final_DUA_m_pt = (9999,9999,9999,9999)
        Final_DUA_u_pt = (9999,9999,9999,9999)
        Final_DUA_ut_pt = (9999,9999,9999,9999)
        im = None
        global HISTORY_VLA_pt
        # Plot Detect results
        if pred_boxes and show_boxes:
            
            for d in reversed(pred_boxes):
                c, conf, id = int(d.cls), float(d.conf) if conf else None, None if d.id is None else int(d.id.item())
                name = ('' if id is None else f'id:{id} ') + names[c]
                label = (f'{name} {conf:.2f}' if conf else name) if labels else None
                # annotator.box_label(d.xyxy.squeeze(), label, color=colors(c, True))
                # Alister add 2024-01-05
                VLA_pt,DCA_pt,VPA_pt,DUA_d_pt,DUA_m_pt,DUA_u_pt,DUA_ut_pt,im = annotator.box_label(d.xyxy.squeeze(), label, color=colors(c, True))
                # print("[result.py]Alister 2024-01-04")
                if VLA_pt[0]!=9999:
                    Final_VLA_pt = VLA_pt
                    HISTORY_VLA_pt = VLA_pt
                if DCA_pt[0]!=9999:
                    Final_DCA_pt = DCA_pt
                if VPA_pt[0]!=9999:
                    Final_VPA_pt = VPA_pt
                if DUA_d_pt[0]!=9999:
                    Final_DUA_d_pt = DUA_d_pt
                if DUA_m_pt[0]!=9999:
                    Final_DUA_m_pt = DUA_m_pt
                if DUA_u_pt[0]!=9999:
                    Final_DUA_u_pt = DUA_u_pt
                if DUA_ut_pt[0]!=9999:
                    Final_DUA_ut_pt = DUA_ut_pt

                   
        l_p1 = None
        r_p1 = None
        l_p2 = None
        r_p2 = None
        l_p3 = None
        r_p3 = None
        l_p4 = None
        r_p4 = None
        l_p5 = None
        r_p5 = None
        l_vl = None
        r_vl = None
        vp = None


        if Final_DCA_pt[0]!=9999:
            l_p1 = (Final_DCA_pt[0],Final_DCA_pt[1])
            r_p1 = (Final_DCA_pt[2],Final_DCA_pt[3])
        if Final_DUA_d_pt[0]!=9999:
            l_p2 = (Final_DUA_d_pt[0],Final_DUA_d_pt[1])
            r_p2 = (Final_DUA_d_pt[2],Final_DUA_d_pt[3])
        if Final_DUA_m_pt[0]!=9999:
            l_p3 = (Final_DUA_m_pt[0],Final_DUA_m_pt[1])
            r_p3 = (Final_DUA_m_pt[2],Final_DUA_m_pt[3])
        if Final_DUA_u_pt[0]!=9999:
            l_p4 = (Final_DUA_u_pt[0],Final_DUA_u_pt[1])
            r_p4 = (Final_DUA_u_pt[2],Final_DUA_u_pt[3])
        if Final_DUA_ut_pt[0]!=9999:
            l_p5 = (Final_DUA_ut_pt[0],Final_DUA_ut_pt[1])
            r_p5 = (Final_DUA_ut_pt[2],Final_DUA_ut_pt[3])
        
        if Final_VLA_pt[0]!=9999:
            l_vl = (Final_VLA_pt[0],Final_VLA_pt[1])
            r_vl = (Final_VLA_pt[2],Final_VLA_pt[3])
            # l_vl = (Final_VLA_pt[0],Final_VLA_pt[1]-40)
            # r_vl = (Final_VLA_pt[2],Final_VLA_pt[3]-40)
        
        if Final_VLA_pt[0]!=9999 and Final_VPA_pt[0]!=9999:
            vp = (int((Final_VPA_pt[0]+Final_VPA_pt[2])/2.0),Final_VLA_pt[1])
        
        # if l_p1 is None:
        #     p1 = (0,0)
        # else: 
        #     p1 = (l_p1,r_p1)

        # p2 = (l_p2,r_p2) if l_p2 is not None else (0,0)
        # p3 = (l_p3,r_p3) if l_p3 is not None else (0,0)
        # p4 = (l_p4,r_p4) if l_p4 is not None else (0,0)
        # p5 = (l_p5,r_p5) if l_p5 is not None else (0,0)

        p1 = (l_p1,r_p1)
        p2 = (l_p2,r_p2)
        p3 = (l_p3,r_p3)
        p4 = (l_p4,r_p4)
        p5 = (l_p5,r_p5)
        ADAS_Key_Points = (p1,p2,p3,p4,p5)
        if pred_boxes and show_boxes:
            for d in reversed(pred_boxes):
                c, conf, id = int(d.cls), float(d.conf) if conf else None, None if d.id is None else int(d.id.item())
                name = ('' if id is None else f'id:{id} ') + names[c]
                label = (f'{name} {conf:.2f}' if conf else name) if labels else None
                if HISTORY_VLA_pt is not None:
                    annotator.box_FCWS_label(d.xyxy.squeeze(), ADAS_Key_Points, HISTORY_VLA_pt[1], label, color=colors(c, True))

        DRAW_MIDDLE_LINE = True
        DRAW_LEFT_LINE = True
        DRAW_RIGHT_LINE = True
        DRAW_CENTER_LINE = True
        DRAW_LDWS = True
        DRAW_VANISH_LINE = True
        DRAW_VANISH_POINT= False
        ## Draw Vanish Line
        if DRAW_VANISH_LINE:
            if l_vl is not None and r_vl is not None:
                cv2.line(im, l_vl, r_vl, (255,0,200), 1)

        ## Draw Left Line
        color = (127,255,0)
        color_m = (255,127)
        thickness = 2
        thickness_m = 2
        thickness_vp = 1
        if l_p1 is not None and l_p2 is not None:
            if DRAW_LEFT_LINE:
                cv2.line(im, l_p1, l_p2, color, thickness)
            ## Draw Middle Line
            if DRAW_MIDDLE_LINE:
                m_p1 = (int((Final_DCA_pt[0]+Final_DCA_pt[2])/2.0),Final_DCA_pt[3])
                m_p2 = (int((Final_DUA_d_pt[0]+Final_DUA_d_pt[2])/2.0),Final_DUA_d_pt[3])
                cv2.line(im, m_p1, m_p2, color_m, thickness_m)
        if l_p2 is not None and l_p3 is not None:
            if DRAW_LEFT_LINE:
                cv2.line(im, l_p2, l_p3, color, thickness)
            ## Draw Middle Line
            if DRAW_MIDDLE_LINE:
                m_p2 = (int((Final_DUA_d_pt[0]+Final_DUA_d_pt[2])/2.0),Final_DUA_d_pt[3])
                m_p3 = (int((Final_DUA_m_pt[0]+Final_DUA_m_pt[2])/2.0),Final_DUA_m_pt[3])
                cv2.line(im, m_p2, m_p3, color_m, thickness_m)
        if l_p3 is not None and l_p4 is not None:
            if DRAW_LEFT_LINE:
                cv2.line(im, l_p3, l_p4, color, thickness)
            ## Draw Middle Line
            if DRAW_MIDDLE_LINE:
                m_p3 = (int((Final_DUA_m_pt[0]+Final_DUA_m_pt[2])/2.0),Final_DUA_m_pt[3])
                m_p4 = (int((Final_DUA_u_pt[0]+Final_DUA_u_pt[2])/2.0),Final_DUA_u_pt[3])
                cv2.line(im, m_p3, m_p4, color_m, thickness_m)
        if l_p4 is not None and l_p5 is not None:
            if DRAW_LEFT_LINE:
                #print("l_p4 is not None and l_p5 is not None")
                cv2.line(im, l_p4, l_p5, color, thickness)
            ## Draw Middle Line
            if DRAW_MIDDLE_LINE:
                m_p4 = (int((Final_DUA_u_pt[0]+Final_DUA_u_pt[2])/2.0),Final_DUA_u_pt[3])
                m_p5 = (int((Final_DUA_ut_pt[0]+Final_DUA_ut_pt[2])/2.0),Final_DUA_ut_pt[3])
                cv2.line(im, m_p4, m_p5, color_m, thickness_m)
        if DRAW_VANISH_POINT:
            if l_p5 is not None and vp is not None:
                if DRAW_LEFT_LINE:
                    #print("l_p4 is not None and l_p5 is not None")
                    cv2.line(im, l_p5, vp, color, thickness_vp)
                ## Draw Middle Line
                if DRAW_MIDDLE_LINE:
                    m_vp = vp
                    m_p5 = (int((Final_DUA_ut_pt[0]+Final_DUA_ut_pt[2])/2.0),Final_DUA_ut_pt[3])
                    cv2.line(im, m_p5, m_vp, color_m, thickness_vp)
            elif l_p5 is None and vp is not None and l_p4 is not None:
                if DRAW_LEFT_LINE:
                    #print("l_p4 is not None and l_p5 is not None")
                    cv2.line(im, l_p4, vp, color, thickness_vp)
                ## Draw Middle Line
                if DRAW_MIDDLE_LINE:
                    m_vp = vp
                    m_p4 = (int((Final_DUA_u_pt[0]+Final_DUA_u_pt[2])/2.0),Final_DUA_u_pt[3])
                    cv2.line(im, m_p4, m_vp, color_m, thickness_vp)
        # elif l_p4 is not None and l_p5 is None:
        #     print("l_p5 is None")
        # elif l_p4 is  None and l_p5 is not None:
        #     print("l_p4 is None")
        # else:
        #     print("l_p4 is None and l_p5 is None")
        ## Draw Right Line
        color = (0,127,255)
        thickness = 2
        if DRAW_RIGHT_LINE:
            if r_p1 is not None and r_p2 is not None:
                cv2.line(im, r_p1, r_p2, color, thickness)
            if r_p2 is not None and r_p3 is not None:
                cv2.line(im, r_p2, r_p3, color, thickness)
            if r_p3 is not None and r_p4 is not None:
                cv2.line(im, r_p3, r_p4, color, thickness)
            if r_p4 is not None and r_p5 is not None:
                cv2.line(im, r_p4, r_p5, color, thickness)
        if DRAW_VANISH_POINT:
            if r_p5 is not None and vp is not None:
                cv2.line(im, r_p5, vp, color, thickness)
            elif r_p5 is None and vp is not None and r_p4 is not None:
                cv2.line(im, r_p4, vp, color, thickness_vp)
        if im is not None:
            h,w = im.shape[0],im.shape[1]
            ## Draw Center Line
            if DRAW_CENTER_LINE:
                c_x = int(w/2.0)
                c_y1 = int(h*0.80)
                c_y2 = int(h*0.99)
                c1 = (c_x,c_y1)
                c2 = (c_x,c_y2)
                cv2.line(im, c1, c2, (255,255,0), 2)
            ## Draw Vanish Point Line
            # Not Implemented

            ## Draw LDWS
            if DRAW_LDWS:
                if l_p1 is not None and r_p1 is not None:
                    LD_TH = int(abs(r_p1[0] - l_p1[0]) / 3.5) 
                    driver_x = int((l_p1[0] + r_p1[0])/2.0)
                    departure_distance = abs(driver_x - int(w/2.0))
                    if departure_distance>LD_TH:
                        text = 'DEPARTURE WARNING !'
                        cv2.putText(im, text, (int(w/8.0), int(h/4.0)), cv2.FONT_HERSHEY_PLAIN,2.5, (0, 0, 255), 4, cv2.LINE_AA)

        # Plot Classify results
        if pred_probs is not None and show_probs:
            text = ',\n'.join(f'{names[j] if names else j} {pred_probs.data[j]:.2f}' for j in pred_probs.top5)
            x = round(self.orig_shape[0] * 0.03)
            annotator.text([x, x], text, txt_color=(255, 255, 255))  # TODO: allow setting colors

        # Plot Pose results
        if self.keypoints is not None:
            for k in reversed(self.keypoints.data):
                annotator.kpts(k, self.orig_shape, radius=kpt_radius, kpt_line=kpt_line)

        # Plot ADAS Segmentation results
        img = annotator.result()
        # if self.drive_map is not None:
        #     self.drive_map = np.squeeze(self.drive_map.detach().cpu().numpy())
        #     # self.drive_raw = cls_to_color(self.drive_map, 'drive')
        #     self.drive_map = cv2.resize(self.drive_map, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)
        #     self.drive_map = cls_to_color(self.drive_map, 'drive')
        #     img[self.drive_map != 0] = img[self.drive_map != 0] * 0.5 + self.drive_map[self.drive_map != 0] * 0.5
        # if self.lane_map is not None:
        #     self.lane_map = np.squeeze(self.lane_map.detach().cpu().numpy())
        #     # self.lane_raw = cls_to_color(self.lane_map, 'lane')
        #     self.lane_map = cv2.resize(self.lane_map, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)
        #     self.lane_map = cls_to_color(self.lane_map, 'lane')
        #     img[self.lane_map != 0] = img[self.lane_map != 0] * 0.5 + self.lane_map[self.lane_map != 0] * 0.5
        # if self.seg_map is not None:
        #     self.seg_map = np.squeeze(self.seg_map.detach().cpu().numpy())
        #     self.seg_map = cv2.resize(self.seg_map, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)
        #     self.seg_map = cls_to_color(self.seg_map, 'seg')
        #     img[self.seg_map != 0] = img[self.seg_map != 0] * 0.5 + self.seg_map[self.seg_map != 0] * 0.5
        return img, self.drive_map, self.lane_map

    def verbose(self):
        """Return log string for each task."""
        log_string = ''
        probs = self.probs
        boxes = self.boxes
        if len(self) == 0:
            return log_string if probs is not None else f'{log_string}(no detections), '
        if probs is not None:
            log_string += f"{', '.join(f'{self.names[j]} {probs.data[j]:.2f}' for j in probs.top5)}, "
        if boxes:
            for c in boxes.cls.unique():
                n = (boxes.cls == c).sum()  # detections per class
                log_string += f"{n} {self.names[int(c)]}{'s' * (n > 1)}, "
        return log_string

    def save_txt(self, txt_file, save_conf=False):
        """
        Save predictions into txt file.

        Args:
            txt_file (str): txt file path.
            save_conf (bool): save confidence score or not.
        """
        boxes = self.boxes
        masks = self.masks
        probs = self.probs
        kpts = self.keypoints
        texts = []
        if probs is not None:
            # Classify
            [texts.append(f'{probs.data[j]:.2f} {self.names[j]}') for j in probs.top5]
        elif boxes:
            # Detect/segment/pose
            for j, d in enumerate(boxes):
                c, conf, id = int(d.cls), float(d.conf), None if d.id is None else int(d.id.item())
                line = (c, *d.xywhn.view(-1))
                if masks:
                    seg = masks[j].xyn[0].copy().reshape(-1)  # reversed mask.xyn, (n,2) to (n*2)
                    line = (c, *seg)
                if kpts is not None:
                    kpt = torch.cat((kpts[j].xyn, kpts[j].conf[..., None]), 2) if kpts[j].has_visible else kpts[j].xyn
                    line += (*kpt.reshape(-1).tolist(), )
                line += (conf, ) * save_conf + (() if id is None else (id, ))
                texts.append(('%g ' * len(line)).rstrip() % line)

        if texts:
            Path(txt_file).parent.mkdir(parents=True, exist_ok=True)  # make directory
            with open(txt_file, 'a') as f:
                f.writelines(text + '\n' for text in texts)

    def save_crop(self, save_dir, file_name=Path('im.jpg')):
        """
        Save cropped predictions to `save_dir/cls/file_name.jpg`.

        Args:
            save_dir (str | pathlib.Path): Save path.
            file_name (str | pathlib.Path): File name.
        """
        if self.probs is not None:
            LOGGER.warning('WARNING ⚠️ Classify task do not support `save_crop`.')
            return
        for d in self.boxes:
            save_one_box(d.xyxy,
                         self.orig_img.copy(),
                         file=Path(save_dir) / self.names[int(d.cls)] / f'{Path(file_name).stem}.jpg',
                         BGR=True)

    def tojson(self, normalize=False):
        """Convert the object to JSON format."""
        if self.probs is not None:
            LOGGER.warning('Warning: Classify task do not support `tojson` yet.')
            return

        import json

        # Create list of detection dictionaries
        results = []
        data = self.boxes.data.cpu().tolist()
        h, w = self.orig_shape if normalize else (1, 1)
        for i, row in enumerate(data):  # xyxy, track_id if tracking, conf, class_id
            box = {'x1': row[0] / w, 'y1': row[1] / h, 'x2': row[2] / w, 'y2': row[3] / h}
            conf = row[-2]
            class_id = int(row[-1])
            name = self.names[class_id]
            result = {'name': name, 'class': class_id, 'confidence': conf, 'box': box}
            if self.boxes.is_track:
                result['track_id'] = int(row[-3])  # track ID
            if self.masks:
                x, y = self.masks.xy[i][:, 0], self.masks.xy[i][:, 1]  # numpy array
                result['segments'] = {'x': (x / w).tolist(), 'y': (y / h).tolist()}
            if self.keypoints is not None:
                x, y, visible = self.keypoints[i].data[0].cpu().unbind(dim=1)  # torch Tensor
                result['keypoints'] = {'x': (x / w).tolist(), 'y': (y / h).tolist(), 'visible': visible.tolist()}
            results.append(result)

        # Convert detections to JSON
        return json.dumps(results, indent=2)


class Boxes(BaseTensor):
    """
    A class for storing and manipulating detection boxes.

    Args:
        boxes (torch.Tensor | numpy.ndarray): A tensor or numpy array containing the detection boxes,
            with shape (num_boxes, 6) or (num_boxes, 7). The last two columns contain confidence and class values.
            If present, the third last column contains track IDs.
        orig_shape (tuple): Original image size, in the format (height, width).

    Attributes:
        xyxy (torch.Tensor | numpy.ndarray): The boxes in xyxy format.
        conf (torch.Tensor | numpy.ndarray): The confidence values of the boxes.
        cls (torch.Tensor | numpy.ndarray): The class values of the boxes.
        id (torch.Tensor | numpy.ndarray): The track IDs of the boxes (if available).
        xywh (torch.Tensor | numpy.ndarray): The boxes in xywh format.
        xyxyn (torch.Tensor | numpy.ndarray): The boxes in xyxy format normalized by original image size.
        xywhn (torch.Tensor | numpy.ndarray): The boxes in xywh format normalized by original image size.
        data (torch.Tensor): The raw bboxes tensor (alias for `boxes`).

    Methods:
        cpu(): Move the object to CPU memory.
        numpy(): Convert the object to a numpy array.
        cuda(): Move the object to CUDA memory.
        to(*args, **kwargs): Move the object to the specified device.
    """

    def __init__(self, boxes, orig_shape) -> None:
        """Initialize the Boxes class."""
        if boxes.ndim == 1:
            boxes = boxes[None, :]
        n = boxes.shape[-1]
        assert n in (6, 7), f'expected `n` in [6, 7], but got {n}'  # xyxy, track_id, conf, cls
        super().__init__(boxes, orig_shape)
        self.is_track = n == 7
        self.orig_shape = orig_shape

    @property
    def xyxy(self):
        """Return the boxes in xyxy format."""
        return self.data[:, :4]

    @property
    def conf(self):
        """Return the confidence values of the boxes."""
        return self.data[:, -2]

    @property
    def cls(self):
        """Return the class values of the boxes."""
        return self.data[:, -1]

    @property
    def id(self):
        """Return the track IDs of the boxes (if available)."""
        return self.data[:, -3] if self.is_track else None

    @property
    @lru_cache(maxsize=2)  # maxsize 1 should suffice
    def xywh(self):
        """Return the boxes in xywh format."""
        return ops.xyxy2xywh(self.xyxy)

    @property
    @lru_cache(maxsize=2)
    def xyxyn(self):
        """Return the boxes in xyxy format normalized by original image size."""
        xyxy = self.xyxy.clone() if isinstance(self.xyxy, torch.Tensor) else np.copy(self.xyxy)
        xyxy[..., [0, 2]] /= self.orig_shape[1]
        xyxy[..., [1, 3]] /= self.orig_shape[0]
        return xyxy

    @property
    @lru_cache(maxsize=2)
    def xywhn(self):
        """Return the boxes in xywh format normalized by original image size."""
        xywh = ops.xyxy2xywh(self.xyxy)
        xywh[..., [0, 2]] /= self.orig_shape[1]
        xywh[..., [1, 3]] /= self.orig_shape[0]
        return xywh


class Masks(BaseTensor):
    """
    A class for storing and manipulating detection masks.

    Attributes:
        xy (list): A list of segments in pixel coordinates.
        xyn (list): A list of normalized segments.

    Methods:
        cpu(): Returns the masks tensor on CPU memory.
        numpy(): Returns the masks tensor as a numpy array.
        cuda(): Returns the masks tensor on GPU memory.
        to(device, dtype): Returns the masks tensor with the specified device and dtype.
    """

    def __init__(self, masks, orig_shape) -> None:
        """Initialize the Masks class with the given masks tensor and original image shape."""
        if masks.ndim == 2:
            masks = masks[None, :]
        super().__init__(masks, orig_shape)

    @property
    @lru_cache(maxsize=1)
    def xyn(self):
        """Return normalized segments."""
        return [
            ops.scale_coords(self.data.shape[1:], x, self.orig_shape, normalize=True)
            for x in ops.masks2segments(self.data)]

    @property
    @lru_cache(maxsize=1)
    def xy(self):
        """Return segments in pixel coordinates."""
        return [
            ops.scale_coords(self.data.shape[1:], x, self.orig_shape, normalize=False)
            for x in ops.masks2segments(self.data)]


class Keypoints(BaseTensor):
    """
    A class for storing and manipulating detection keypoints.

    Attributes:
        xy (torch.Tensor): A collection of keypoints containing x, y coordinates for each detection.
        xyn (torch.Tensor): A normalized version of xy with coordinates in the range [0, 1].
        conf (torch.Tensor): Confidence values associated with keypoints if available, otherwise None.

    Methods:
        cpu(): Returns a copy of the keypoints tensor on CPU memory.
        numpy(): Returns a copy of the keypoints tensor as a numpy array.
        cuda(): Returns a copy of the keypoints tensor on GPU memory.
        to(device, dtype): Returns a copy of the keypoints tensor with the specified device and dtype.
    """

    @smart_inference_mode()  # avoid keypoints < conf in-place error
    def __init__(self, keypoints, orig_shape) -> None:
        """Initializes the Keypoints object with detection keypoints and original image size."""
        if keypoints.ndim == 2:
            keypoints = keypoints[None, :]
        if keypoints.shape[2] == 3:  # x, y, conf
            mask = keypoints[..., 2] < 0.5  # points with conf < 0.5 (not visible)
            keypoints[..., :2][mask] = 0
        super().__init__(keypoints, orig_shape)
        self.has_visible = self.data.shape[-1] == 3

    @property
    @lru_cache(maxsize=1)
    def xy(self):
        """Returns x, y coordinates of keypoints."""
        return self.data[..., :2]

    @property
    @lru_cache(maxsize=1)
    def xyn(self):
        """Returns normalized x, y coordinates of keypoints."""
        xy = self.xy.clone() if isinstance(self.xy, torch.Tensor) else np.copy(self.xy)
        xy[..., 0] /= self.orig_shape[1]
        xy[..., 1] /= self.orig_shape[0]
        return xy

    @property
    @lru_cache(maxsize=1)
    def conf(self):
        """Returns confidence values of keypoints if available, else None."""
        return self.data[..., 2] if self.has_visible else None


class Probs(BaseTensor):
    """
    A class for storing and manipulating classification predictions.

    Attributes:
        top1 (int): Index of the top 1 class.
        top5 (list[int]): Indices of the top 5 classes.
        top1conf (torch.Tensor): Confidence of the top 1 class.
        top5conf (torch.Tensor): Confidences of the top 5 classes.

    Methods:
        cpu(): Returns a copy of the probs tensor on CPU memory.
        numpy(): Returns a copy of the probs tensor as a numpy array.
        cuda(): Returns a copy of the probs tensor on GPU memory.
        to(): Returns a copy of the probs tensor with the specified device and dtype.
    """

    def __init__(self, probs, orig_shape=None) -> None:
        """Initialize the Probs class with classification probabilities and optional original shape of the image."""
        super().__init__(probs, orig_shape)

    @property
    @lru_cache(maxsize=1)
    def top1(self):
        """Return the index of top 1."""
        return int(self.data.argmax())

    @property
    @lru_cache(maxsize=1)
    def top5(self):
        """Return the indices of top 5."""
        return (-self.data).argsort(0)[:5].tolist()  # this way works with both torch and numpy.

    @property
    @lru_cache(maxsize=1)
    def top1conf(self):
        """Return the confidence of top 1."""
        return self.data[self.top1]

    @property
    @lru_cache(maxsize=1)
    def top5conf(self):
        """Return the confidences of top 5."""
        return self.data[self.top5]
