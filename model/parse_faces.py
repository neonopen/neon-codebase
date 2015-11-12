'''
Implements a class that handles parsing of images
and manages the extraction of facial components.

It needs the detector (which finds the face) and
the predictor (which locates facial components) to
be provided when it is initialized. 
'''
import numpy as np
import cv2
import os
import dlib
from utils.pycvutils import ImagePrep

comp_dict = {}
comp_dict['face'] = range(17)
comp_dict['r eyebrow'] = range(17, 22)
comp_dict['l eyebrow'] = range(22, 27)
comp_dict['nose'] = range(27, 36)
comp_dict['r eye'] = range(36, 42)
comp_dict['l eye'] = range(42, 48)
comp_dict['mouth'] = range(48, 68)

class ParseStateError(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        if self.value == 0:
            return "No image analyzed"
        if self.value == 1:
            return "Invalid face index requested"
        if self.value == 2:
            return "Invalid component requested"

class DetectFaces(object):
    '''
    Detects faces.
    '''
    def __init__(self):
        self.detector = dlib.get_frontal_face_detector()
        self.prep = ImagePrep(convert_to_gray=True)

    def get_N_faces(self, image):
        '''
        Returns the number of faces an image contains.
        '''
        image = self.prep(image)
        return len(self.detector(image))

class MultiStageFaceParser(object):
    '''
    Wraps FindAndParseFaces, but allows three-stage evaluation.
    This is necessary because the detection of faces has been
    split from the scoring of closed eyes. Since the segmenting
    faces requires faces to be detected, and scoring eyes requires
    the faces be segmented, this allows us to make multiple evaluations
    within-step and across-images without having to repeat either
    detection or segmentation steps, which saves us time.

    This is designed to work just like any other feature extractor--
    it accepts an image on each call--however, it keeps the MD5 of
    those images cached so that it can look for them again.

    One potentially problematic area is if the images are preprocessed
    differently in the face detection vs. the closed eye detection
    steps. This assumes they will stay the same! To this end, it has as
    an attribute its own preprocessing object.
    '''

    def __init__(self, predictor, max_height=640):
        self.detector = dlib.get_frontal_face_detector()
        self.fParse = FindAndParseFaces(predictor, self.detector)
        self.image_data = {}
        self.max_height = max_height
        self.prep = ImagePrep(
                        max_height=self.max_height)

    def reset(self):
        self.image_data = {}

    def get_faces(self, image):
        ihash = hash(image.tostring())
        if self.image_data.has_key(ihash):
            return self.image_data[ihash]
        det = self.fParse._SEQfindFaces(image)
        self.image_data[ihash] = [det]
        return len(det)

    def get_seg(self, image):
        '''
        Performs segmentation, but does not return anything
        (at the moment). This must be queried in the order
        in which the images were originally submitted to the
        detector, but can otherwise tolerate dropped frames.
        '''
        # find the image index
        ihash = hash(image.tostring())
        if self.image_data.has_key(ihash):
            if len(self.image_data[ihash] > 1):
                return self.image_data[ihash][1]
        else:
            self.get_faces(image)
        det = self.image_data[ihash][0]
        points = self.fParse._SEQsegFaces(image, det)
        self.image_data[ihash].append(points)

    def get_eyes(self, image):
        '''
        Obtains all the eyes for an image.
        '''
        self.get_seg(image)
        return self.fParse.get_all(['l eye', 'r eye'])


class FindAndParseFaces(object):
    '''
    Detects faces, and segments them.
    '''
    def __init__(self, predictor, detector=None):
        if detector == None:
            self.detector = dlib.get_frontal_face_detector()
        else:
            self.detector = detector
        self.predictor = predictor
        self._faceDets = []
        self._facePoints = []
        self._image = None
        self.prep = utils.pycvutils.ImagePrep(convert_to_gray=True)

    def _check_valid(self, face=None, comp=None):
        '''
        Generally checks to see if a request is valid
        '''
        if self._image == None:
            raise ParseStateError(0)
        if not len(self._faceDets):
            return False
        if face != None:
            if type(face) != int:
                raise ValueError("Face index must be an integer")
            if face >= len(self._faceDets):
                raise ParseStateError(1) 
            if face < 0:
                raise ParseStateError(1)
        if comp != None:
            if type(comp) != str:
                raise ValueError("Component key must be string")
            if not comp_dict.has_key(comp):
                raise ParseStateError(2)
        return True

    def _getSquareBB(self, points):
        '''
        Returns a square bounding box given the centroid
        and maximum x or y distance.

        points is a list of [(x, y), ...] pairs.
        '''
        points = np.array(points)
        x_dists = np.abs(points[:,0][:,None]-points[:,0][None,:])
        y_dists = np.abs(points[:,1][:,None]-points[:,1][None,:])
        mx = max(np.max(x_dists), np.max(y_dists))
        cntr_x = np.mean(points[:,0])
        cntr_y = np.mean(points[:,1])
        top = int(cntr_x - mx * 0.5)
        left = int(cntr_y - mx * 0.5)
        return (top, left, mx, mx)

    def _get_points(self, shape, comp):
        '''
        Gets the points that correspond to a given component
        '''
        xypts = []
        points = [shape.part(x) for x in range(shape.num_parts)]
        for pidx in comp_dict[comp]:
            p = points[pidx]
            xypts.append([p.x, p.y])
        return xypts

    def _extract(self, shape, comp):
        '''
        Returns a subimage containing the requested component
        '''
        points = self._get_points(shape, comp)
        top, left, height, width = self._getSquareBB(points)
        return self._image[left:left+width, top:top+height]

    def ingest(self, image):
        image = self.prep(image)
        self._image = image
        self._faceDets = self.detector(image)
        self._facePoints = []
        for f in self._faceDets:
            self._facePoints.append(self.predictor(image, f))

    def _SEQfindFaces(self, image):
        '''
        Detects faces, returns detections to be used by
        MultiStageFaceParser.
        '''
        self._image = self.prep(image)
        return self.detector(image)

    def _SEQsegFaces(self, image, dets):
        '''
        Returns segmented face points given the image and
        the detections, to be used by MultiStageFaceParser
        '''
        self._faceDets = dets
        self._image = self.prep(image)
        self._facePoints = []
        for f in self._faceDets:
            self._facePoints.append(self.predictor(image, f))
        return self._facePoints

    def _SEQfinStep(self, image, dets, points):
        '''
        Restores the parser to the 'final' configuration,
        as if it had run the entire thing from end-to-end,
        allowing us to proceed with the eye scoring.
        '''
        self._faceDets = dets
        self._image = self.prep(image)
        self._facePoints = facePoints

    def get_comp(self, face, comp):
        '''
        Given a face index and a component key, returns the
        sub-image.
        '''
        if not self._check_valid(face, comp):
            return None
        shape = self._facePoints[face]
        return self._extract(shape, comp)

    def get_N_faces(self):
        return len(self._faceDets)

    def get_face(self, face):
        '''
        Returns the subimage that contains a face
        at a given facial idx
        '''
        if not self._check_valid(face):
            return None
        det = self._faceDets[face]
        L, T, R, B = det.left(), det.top(), det.right(), det.bottom()
        simg = self._image[T:B,L:R]
        return simg

    def get_comp_pts(self, face, comp):
        '''
        Given a face index and a component key, returns the
        points.
        '''
        if not self._check_valid(face, comp):
            return None
        shape = self._facePoints[face]
        return self._get_points(shape, comp)

    def iterate_all(self, comp):
        '''
        Given a list of components, returns a generator
        that sequentially yields all the sub images.

        If comp is not a list, it will be converted into
        one.
        '''
        if type(comp) != list:
            comp = [comp]
        for face in range(len(self._facePoints)):
            for c in comp:
                yield self.get_comp(face, c)

    def get_all(self, comp):
        '''
        Similar to iterate_all, only returns the values as
        a list.
        '''
        rcomps = []
        if type(comp) != list:
            comp = [comp]
        for face in range(len(self._facePoints)):
            for c in comp:
                rcomps.append(self.get_comp(face, c))
        return rcomps