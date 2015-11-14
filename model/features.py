'''Feature generators


Copyright: 2013 Neon Labs
Author: Mark Desnoyer (desnoyer@neon-lab.com)
'''
import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

import cv2
import hashlib
import leargist
import logging
import numpy as np
import os
import os.path
from . import TextDetectionPy
import utils.obj
import utils.pycvutils
from model.colorname import ColorName
from model.parse_faces import DetectFaces, FindAndParseFaces
from score_eyes import ScoreEyes

_log = logging.getLogger(__name__)

class FeatureGenerator(object):
    '''Abstract class for the feature generator.'''
    def __init__(self):
        self.__version__ = 2

    def __str__(self):
        return utils.obj.full_object_str(self)

    def reset(self):
        pass

    def generate(self, image):
        '''Creates a feature vector for an image.

        Input:
        image - image to generate features for as a numpy array in BGR
                format (aka OpenCV)

        Returns: 1D numpy feature vector
        '''
        raise NotImplementedError()

    def hash_type(self, hashobj):
        '''Updates a hash object with data about the type.'''
        hashobj.update(self.__class__.__name__)

class RegionFeatureGenerator(FeatureGenerator):
    '''
    Abstract class for a region feature generator, which
    replicates the functionality of FeatureGenerator but over
    a list of images
    '''
    def __init__(self):
        super(RegionFeatureGenerator, self).__init__()
        self.__version__ = 1
        self.max_height = None
        self.crop_frac = None

    def generate_many(self, images, fonly=False):
        '''
        Creates a feature vector for list of images.

        Input:
        images - a list of N images in openCV BGR format.
        fonly - defaults to False. First only: compute this feature
                only for the first image obtained. In the case of
                features that require more than one frame to be
                computed properly (i.e., SAD), the quantity is computed
                with the minimal number of frames required.
        Returns: 
            1D/2D numpy feature object of N[xF] elements,
            where F is the number of features.
        '''
        raise NotImplementedError()

    def get_feat_name(self):
        raise NotImplementedError()

class PredictedFeatureGenerator(FeatureGenerator):
    '''Wrapper around a Predictor so that it looks like a feature generator.'''
    def __init__(self, predictor):
        super(PredictedFeatureGenerator, self).__init__()
        self.predictor = predictor

    def reset(self):
        self.predictor.reset()

    def generate(self, image):
        return self.predictor.predict(image)

    def hash_type(self, hashobj):
        hashobj.update(self.__class__.__name__)
        self.predictor.hash_type(hashobj)

class GistGenerator(FeatureGenerator):
    '''Class that generates GIST features.'''
    def __init__(self, image_size=(144,256)):
        super(GistGenerator, self).__init__()
        self.image_size = image_size

    def __cmp__(self, other):
        typediff = cmp(self.__class__.__name__, other.__class__.__name__)
        if typediff <> 0:
            return typediff
        return cmp(self.image_size, other.image_size)

    def __hash__(self):
        return hash(self.image_size)

    def generate(self, image):
        # leargist needs a PIL image in RGB format
        rimage = utils.pycvutils.resize_and_crop(image, self.image_size[0],
                                                 self.image_size[1])
        pimage = utils.pycvutils.to_pil(rimage)
        return leargist.color_gist(pimage)

class ColorNameGenerator(FeatureGenerator):
    '''Class that generates ColorName features.'''
    def __init__(self, max_height = 480 ):
        super(ColorNameGenerator, self).__init__()
        self.max_height = max_height

    def __cmp__(self, other):
        typediff = cmp(self.__class__.__name__, other.__class__.__name__)
        if typediff <> 0:
            return typediff
        return cmp(self.max_height, other.max_height)

    def __hash__(self):
        return hash(self.max_height)

    def generate(self, image):
        image_size = (int(2*round(float(image.shape[1]) * 
                                self.max_height / image.shape[0] /2)),
                                self.max_height)
        image_resized = cv2.resize(image, image_size)
        return ColorName(image_resized)._hist
        return cn.get_colorname_histogram()

class BlurGenerator(RegionFeatureGenerator):
    '''
    Quantizes the blurriness of a sequence of images.
    '''
    def __init__(self, max_height=512, crop_frac=[0.,0.,0.25,0.]):
        super(BlurGenerator, self).__init__()
        self.max_height = max_height
        self.crop_frac = crop_frac
        self.prep = utils.pycvutils.ImagePrep(
                        max_height=self.max_height,
                        crop_frac=self.crop_frac)

    def __cmp__(self, other):
        typediff = cmp(self.__class__.__name__, other.__class__.__name__)
        if typediff <> 0:
            return typediff
        return cmp(self.max_height, other.max_height)

    def __hash__(self):
        return hash(self.max_height)

    def generate_many(self, images, fonly=False):
        if not type(images) == list:
            images = [images]
        if fonly:
            images = images[:1]
        feat_vec = []
        for img in images:
            img = self.prep(img)
            feat_vec.append(self._comp_blur(img))
        return np.array(feat_vec)

    def _comp_blur(self, image):
        '''
        Computes the blur as the variance of the laplacian. 
        '''
        return cv2.Laplacian(image, cv2.CV_32F).var()

    def get_feat_name(self):
        return 'blur'

class SADGenerator(RegionFeatureGenerator):
    '''
    Generates the sum of absolute differences, or SAD score,
    for a sequence of frames. The first frame receives a score
    of 0. This computes SAD for both forward and backward frames,
    with the first and last frame getting the SAD value for 0 to 1 
    and -2 to -1, respectively
    '''
    def __init__(self, max_height=512, crop_frac=[0.,0.,0.25,0.]):
        super(SADGenerator, self).__init__()
        self.max_height = max_height
        self.crop_frac = crop_frac
        self.prep = utils.pycvutils.ImagePrep(
                        max_height=self.max_height,
                        crop_frac=self.crop_frac)

    def __cmp__(self, other):
        typediff = cmp(self.__class__.__name__, other.__class__.__name__)
        if typediff <> 0:
            return typediff
        return cmp(self.max_height, other.max_height)

    def __hash__(self):
        return hash(self.max_height)

    def generate_many(self, images, fonly=False):
        if not type(images) == list:
            images = [images]
        if fonly:
            images = images[:2]
        images = [self.prep(x) for x in images]
        SAD_vals = self._compute_SAD(images)
        feat_vec = [float(SAD_vals[0])]
        for i in range(0, len(SAD_vals)-1):
            feat_vec.append((SAD_vals[i]+SAD_vals[i+1])/2.)
        feat_vec.append(float(SAD_vals[-1]))
        if fonly:
            feat_vec = feat_vec[:1]
        return np.array(feat_vec)

    def _compute_SAD(self, images):
        SAD_vals = []
        prev_img = images[0]
        for next_img in images[1:]:
            sad = np.sum(cv2.absdiff(prev_img, next_img))
            SAD_vals.append(sad)
            prev_img = next_img
        return SAD_vals

    def get_feat_name(self):
        return 'sad'

class FaceGenerator(RegionFeatureGenerator):
    '''
    Returns a boolean which indicates whether or not a face
    was detected in each grame given a sequence of frames.
    '''
    def __init__(self, MSFP):
        '''
        MSFP is a multi-stage face parser, which has as
        an attribute the preprocessor--this ensures that
        the images passed to FaceGenerator and the images
        passed to ClosedEyeGenerator are preprocessed
        in the same way. Otherwise, the images cannot be
        matched with each other.
        '''
        super(FaceGenerator, self).__init__()
        self.MSFP = MSFP
        self.prep = MSFP.prep
        self.max_height = MSFP.max_height

    def __cmp__(self, other):
        typediff = cmp(self.__class__.__name__, other.__class__.__name__)
        if typediff <> 0:
            return typediff
        return cmp(self.max_height, other.max_height)

    def __hash__(self):
        return hash(self.max_height)

    def generate_many(self, images, fonly=False):
        if not type(images) == list:
            images = [images]
        if fonly:
            images = images[:1]
        feat_vec = []
        for img in images:
            img = self.prep(img)
            feat_vec.append(self.MSFP.get_faces(img))
        return np.array(feat_vec)

    def get_feat_name(self):
        return 'faces'

class ClosedEyeGenerator(RegionFeatureGenerator):
    '''
    Returns the distance to the separating hyperplane of the
    'least open eyes' in a sequence of frames.
    '''
    def __init__(self, MSFP, classifier):
        '''
        MSFP is a multi-stage face parser; see FaceGenerator
        for an explanation of why this must be so.
        '''
        super(ClosedEyeGenerator, self).__init__()
        self.MSFP = MSFP
        self.prep = MSFP.prep
        self.max_height = MSFP.max_height
        self.scoreEyes = ScoreEyes(classifier)

    def __cmp__(self, other):
        typediff = cmp(self.__class__.__name__, other.__class__.__name__)
        if typediff <> 0:
            return typediff
        return cmp(self.max_height, other.max_height)

    def __hash__(self):
        return hash(self.max_height)

    def generate_many(self, images, fonly=False):
        if not type(images) == list:
            images = [images]
        if fonly:
            images = images[:1]
        feat_vec = []
        for img in images:
            img = self.prep(img)
            eyes = self.MSFP.get_eyes(img)
            if not len(eyes):
                feat_vec.append(0)
                continue
            classif, scores = self.scoreEyes.classifyScore(eyes)
            feat_vec.append(np.min(scores))
        return np.array(feat_vec)

    def get_feat_name(self):
        return 'eyes'

class LightnessGenerator(RegionFeatureGenerator):
    '''
    Returns the mean darkness in an image.
    '''
    def __init__(self, max_height=480):
        super(DarknessGenerator, self).__init__()
        self.max_height = max_height
        self.prep = utils.pycvutils.ImagePrep(max_height=self.max_height)

    def __cmp__(self, other):
        typediff = cmp(self.__class__.__name__, other.__class__.__name__)
        if typediff <> 0:
            return typediff
        return cmp(self.max_height, other.max_height)

    def __hash__(self):
        return hash(self.max_height)

    def generate_many(self, images, fonly=False):
        if not type(images) == list:
            images = [images]
        if fonly:
            images = images[:1]
        feat_vec = []
        for img in images:
            # check to see if the image is black and white
            if len(img.shape) < 3:
                return np.mean(img)
            elif img.shape[2] == 1:
                return np.mean(img)
            # convert to HSV
            feat_vec.append(np.mean(cv2.cvtColor(img, cv2.cv.BGR2HSV)[:,:,2]))
        return np.array(feat_vec)

    def get_feat_name(self):
        return 'lightness'

class TextGenerator(RegionFeatureGenerator):
    '''
    Returns the quantity of text per frame given a sequence
    of frames.

    Unlike the normal text filter, this does not chop off the
    bottom quadrant (at least, not be default)
    '''
    def __init__(self, max_height=480, crop_frac=None):
        super(TextGenerator, self).__init__()
        self.max_height = max_height
        self.prep = utils.pycvutils.ImagePrep(
                        max_height=self.max_height,
                        crop_frac=crop_frac)

    def __cmp__(self, other):
        typediff = cmp(self.__class__.__name__, other.__class__.__name__)
        if typediff <> 0:
            return typediff
        return cmp(self.max_height, other.max_height)

    def __hash__(self):
        return hash(self.max_height)

    def generate_many(self, images, fonly=False):
        if not type(images) == list:
            images = [images]
        if fonly:
            images = images[:1]
        feat_vec = []
        for img in images:
            img = self.prep(img)
            text_image = TextDetectionPy.TextDetection(img)
            score = (float(np.count_nonzero(text_image)) /
                (text_image.shape[0] * text_image.shape[1]))
            feat_vec.append(score)
        return np.array(feat_vec)

    def get_feat_name(self):
        return 'text'

class PixelVarGenerator(RegionFeatureGenerator):
    '''
    Computes the maximum channelwise variance per image for
    every image in a sequence
    '''
    def __init__(self, max_height=480, crop_frac=.8):
        super(PixelVarGenerator, self).__init__()
        self.max_height = max_height
        self.prep = utils.pycvutils.ImagePrep(
                        max_height=self.max_height,
                        crop_frac=crop_frac)

    def __cmp__(self, other):
        typediff = cmp(self.__class__.__name__, other.__class__.__name__)
        if typediff <> 0:
            return typediff
        return cmp(self.max_height, other.max_height)

    def __hash__(self):
        return hash(self.max_height)

    def generate_many(self, images, fonly=False):
        if not type(images) == list:
            images = [images]
        if fonly:
            images = images[:1]
        feat_vec = []
        for img in images:
            img = self.prep(img)
            feat_vec.append(np.max(np.var(np.var(img,0),0)))
        return np.array(feat_vec)

    def get_feat_name(self):
        return 'pixvar'

class MemCachedFeatures(FeatureGenerator):
    '''Wrapper for a feature generator that caches the features in memory'''
    _shared_instances = {}
    
    def __init__(self, feature_generator):
        super(MemCachedFeatures, self).__init__()
        self.feature_generator = feature_generator
        self.cache = {}
        self._shared = False

    def __str__(self):
        return utils.obj.full_object_str(self, exclude=['cache'])

    def reset(self):
        self.feature_generator.reset()
        self.cache = {}

    def generate(self, image):
        key = hash(image.tostring())

        try:
            return self.cache[key]
        except KeyError:
            features = self.feature_generator.generate(image)
            self.cache[key] = features
            return features

    def __setstate__(self, state):
        '''If this is a shared cache, register it when unpickling.'''
        self.__dict__.update(state)
        if self._shared:
            MemCachedFeatures._shared_instances[self.feature_generator] = self

    @classmethod
    def create_shared_cache(cls, feature_generator):
        '''Factory function to create an in memory cached that can be shared.

        The shared caches are those that have the same feature_generator
        '''
        try:
            return cls._shared_instances[feature_generator]
        except KeyError:
            instance = MemCachedFeatures(feature_generator)
            instance._shared = True
            cls._shared_instances[feature_generator] = instance
            return instance

        return None
        
class DiskCachedFeatures(FeatureGenerator):
    '''Wrapper for a feature generator that caches the features for images on the disk.

    Images are keyed by their md5 hash.
    '''
    def __init__(self, feature_generator, cache_dir=None):
        '''Create the cached generator.
Inputs:
        feature_generator - the generator to cache features for
        cache_dir - Directory to store the cached features in.
                    If None, becomes an in-memory shared cache.
        
        '''
        super(DiskCachedFeatures, self).__init__()
        self.feature_generator = feature_generator

        if cache_dir is not None and not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        self.cache_dir = cache_dir

    @property
    def cache_dir(self):
        return self._cache_dir

    @cache_dir.setter
    def cache_dir(self, cache_dir):
        # When setting cache dir to None, we revert to an in-memory
        # shared class.
        self._cache_dir = cache_dir
        if self._cache_dir == None:
            _log.warning('Using an in memory cache instead of a disk cache.')
            mem_cache = MemCachedFeatures.create_shared_cache(
                self.feature_generator)
            self.feature_generator = mem_cache

    def reset(self):
        self.feature_generator.reset()

    def generate(self, image):
        if self.cache_dir is not None:
            hashobj = hashlib.md5()
            hashobj.update(image.view(np.uint8))
            hashobj.update(str(self.__version__))
            self.feature_generator.hash_type(hashobj)
            hashhex = hashobj.hexdigest()
            cache_file = os.path.join(self.cache_dir, '%s.npy' % hashhex)

            if os.path.exists(cache_file):
                return np.load(cache_file)
            
        features = self.feature_generator.generate(image)

        if self.cache_dir is not None:
            if not os.path.exists(self.cache_dir):
                os.makedirs(self.cache_dir)
            np.save(cache_file, features)

        return features
            

    def __setstate__(self, state):
        '''Extra handling for when this is unpickled.'''
        self.__dict__.update(state)

        # If the cache directory doesn't exist, then turn off caching
        if self.cache_dir is not None and not os.path.exists(self.cache_dir):
            _log.warning('Cache directory %s not found.' % self.cache_dir)
            self.cache_dir = None
