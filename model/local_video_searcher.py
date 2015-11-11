'''
New video searcher. Implements:
    - local search (to circumvent closed-eyes, blurry, etc...)
    - Metropolis-Hastings sampling
    - Inverse filter / score order (3x speedup)

NOTE:
This no longer inherits from the VideoSearcher() object, I'm not
sure if we want to change how this works in the future.

NOTE:
While this initially used Statistics() objects to calculate running
statistics, in principle even with a small search interval (32 frames)
and a very long video (2 hours), we'd only have about 5,000 values to
store, which we can easily manage. Thus we will hand-roll our own Statistics
objects. 
'''

import hashlib
import heapq
import logging
import os
import sys
import threading
import time
import traceback
from Queue import Queue
from itertools import permutations
from collections import OrderedDict as odict

__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

import cv2
import ffvideo
import model.errors
import model.features as feat
import numpy as np
import utils.obj
from model import colorname
from utils import pycvutils, statemon
from utils.pycvutils import seek_video
from model.metropolisHastingsSearch import ThumbnailResultObject
from model.metropolisHastingsSearch import MonteCarloMetropolisHastings

_log = logging.getLogger(__name__)

statemon.define('all_frames_filtered', int)
statemon.define('cv_video_read_error', int)
statemon.define('video_processing_error', int)
statemon.define('low_number_of_frames_seen', int)

MINIMIZE = -1   # flag for statistics where better = smaller
NORMALIZE = 0   # flag for statistics where better = closer to mean
MAXIMIZE = 1    # flag for statistics where better = larger


class Statistics(object):
    '''
    Replicates (to a degree) the functionality of the true running statistics
    objects (which are in the utils folder under runningstat). This is because
    it is unlikely that we will ever need to maintain a very large number of 
    measurements. 

    If init is not None, it initializes with the values provided.
    '''
    def __init__(self, max_size=5000, init=None):
        """
        Parameters:
            max_size = the maximum size of the value array
            init = an initial set of values to instantiate it
        """
        self._count = 0
        self._max_size = max_size
        self._vals = np.zeros(max_size)
        if init is not None:
            self.push(init)

    def push(self, x):
        '''
        pushes a value onto x
        '''
        if type(x) == list:
            for ix in x:
                self.push(ix)
        if self._count == self._max_size:
            # randomly replace one
            idx = np.random.choice(self._max_size)
            self._vals[idx] = x
        else:
            self._vals[self._count] = x
            self._count += 1 # increment count

    def var(self):
        return np.var(self._vals[:self._count])

    def mean(self):
        return np.mean(self._vals[:self._count])

    def rank(self, x):
        '''Returns the rank of x'''
        quant = np.sum(self._vals[:self._count] < x)
        return quant * 1./self._count

class Combiner(object):
    '''
    Combines arbitrary feature vectors according to either (1) predefined
    weights or (2) attempts to deduce the weight given the global statistics
    object.
    '''
    def __init__(self, stats_dict, weight_dict=None,
                 weight_valence=None, combine=lambda x: np.sum(x)):
        '''
        stats_dict is a dictionary of {'stat name': Statistics()}
        weight_dict is a dictionary of {'stat name': weight} which yields
            absolute weights.
        weight_valence is a dictionary of {'stat name': valence} encoding,
            which indicates whether 'better' is higher, lower, or maximally
            typical.
        combine is an anonymous function to combine scored statistics; combine
            must have a single argument and be able to operate on lists of
            floats.
        Note: if a statistic has an entry in both the stats and weights dict,
            then weights dict takes precedence.
        '''
        self._stats_dict = stats_dict
        self.weight_dict = weight_dict
        self.weight_valence = weight_valence
        self._combine = combine

    def _compute_stat_score(self, feat_name, feat_vec):
        '''
        Computes the statistics score for a feature vector. If it has a
        defined weight, then we simply return the product of this weight with
        the value of the feature. 
        '''
        if self.weight_dict.has_key(feat_name):
            return [x * self.weight_dict[feat_name] for x in feat_vec]
        
        if self._stats_dict.has_key(feat_name):
            vals = []
            if self.weight_valence.has_key(feat_name):
                valence = self.weight_valence[feat_name]
            else:
                valence = MINIMIZE # assume you are trying to maximize it
            for v in feat_vec:
                rank = self._stats_dict[feat_name].rank()
                if valence == MINIMIZE:
                    rank = 1. - rank
                if valence == NORMALIZE:
                    rank = 1. - abs(0.5 - rank)*2
                vals.append(rank)
            return vals

        return feat_vec

    def combine_scores(self, feat_dict):
        '''
        Returns the scores for the thumbnails given a feat_dict, which is a
        dictionary {'feature name': feature_vector}
        '''
        stat_scores = []
        for k, v in feat_dict.iteritems():
            stat_score.append(self._compute_stat_score(k, v))
        comb_scores = []
        for x in zip(*stat_score):
            comb_scores.append(self._combine(x))

class _Result(object):
    '''
    Private class to be used by the ResultsList object. Represents an
    invidiual top-result image in the top results list.
    '''
    def __init__(self, frameno=None, score=-np.inf, image=None, 
                 feat_score=None, meta=None):
        self._defined = False
        if self.score:
            self._defined = True
            _log.debug(('Instantiating result object at frame %i with',
                           ' score %.3f')%(frameno, score))
        self.score = score
        self.frameno = frameno
        self._color_name = None
        self._feat_score = feat_score
        self._hash = np.random.getrandbits(128)
        self.image = image
        self.meta = meta

    def __cmp__(self, other):
        if type(self) is not type(other):
            return cmp(self.score, other)
        # undefined result objects are always 'lower' than defined
        # result objects. 
        if not self._defined:
            return -1
        if not other._defined:
            return 1
        return cmp(self.score, other.score)

    def __str__(self):
        if not self._defined:
            return 'Undefined Top Result object'
        return 'Top Result object at %i, score %.2f'%(self.frameno, 
                                                      self.score)

    def dist(self, other):
        if type(self) is not type(other):
            raise ValueError('Must get distance relative to other Result obj')
        if not self._defined:
            if not other._defined:
                return 0
            else:
                return np.inf
        if not other._defined:
            return np.inf
        if self._hash == other._hash:
            return np.inf # the same object is infinitely different from itself
        return self._color_name.dist(other._color_name)

class ResultsList(object):
    '''
    The ResultsList class represents the sorted list of current best results. 
    This also handles the updating of the results list. 

    If the 'max_variety' parameter is set to true (default), inserting a new
    result is not guaranteed to kick out the lowest current scoring result; 
    instead, it's also designed to maximize variety as represented by the 
    histogram of the colorname. Thus, new results added to the pile will not
    be added if the minimium pairwise distance between all results is
    decreased.
    '''
    def __init__(self, n_thumbs=5, max_variety=True):
        self._max_variety = max_variety
        self.n_thumbs = n_thumbs
        self.reset()

    def reset(self):
        _log.debug('Result object of size %i resetting'%(self.n_thumbs))
        self.results = [_Result() for x in range(self.n_thumbs)]
        self.min = 0
        self.dists = np.zeros((self.n_thumbs, self.n_thumbs))

    def _update_dists(self, entry_idx):
        for idx in range(len(self.results)):
            dst = self.results[idx].dist(self.results[entry_idx])
            self.dists[idx, entry_idx] = dst
            self.dists[entry_idx, idx] = dst

    def accept_replace(self, frameno, score, image=None, feat_score=None,
                       meta=None):
        '''
        Attempts to insert a result into the results list. If it does not
        qualify, it returns False, otherwise returns True
        '''
        if score < self.min:
            _log.warn('Frame %i [%.3f] rejected due to score'%(frameno,
                                                                   score))
            _log.warn('This frame should never have been submitted!')
            return False
        res = _Result(frameno, score, image, feat_score, meta)
        if not self._max_variety:
            return self._push_over_lowest(res)
        else:
            return self._maxvar_replace(res)

    def _compute_new_dist(self, res):
        '''
        Returns the distance of the new result object to all result objects
        currently in the list of result objects.
        '''
        dists = []
        for rres in self.results:
            dists.append(res.dist(rres))
        return np.array(dists)

    def _push_over_lowest(self, res):
        '''
        Replaces the current lowest-scoring result with whatever res is. Note:
        this does not check that res's score > the min score. It's assumed
        that this is done in accept_replace.
        '''
        sco_by_idx = np.argsort([x.score for x in self.results])
        self.results[sco_by_idx[0]] = res
        self._update_min()
        return True

    def _maxvar_replace(self, res):
        '''
        Replaces the lowest scoring result possible while maximizing variance.
        '''
        repl_idx = [x for x in range(len(self.results)) if 
                        self.results[x] < res]
        # get dists as they are now
        mdists = np.min(self.dists[repl_idx], 1)
        # get the distances of the candidate to the current results
        dists = self._compute_new_dist(res)
        arg_srt_idx = np.argsort(dists)
        # iterate over the lowest scoring results, and see if you can
        # replace them.
        sco_by_idx = np.argsort([x.score for x in self.results]) 
        for idx in sco_by_idx:
            if self.results[idx].score > res.score:
                # none of the current thumbnails can be replaced
                _log.debug('%s rejected due to similarity'%(res))
                return False
            # see if you can replace it
            if idx == arg_srt_idx[0]:
                c_min_dist = dists[arg_srt_idx[1]]
            else:
                c_min_dist = dists[arg_srt_idx[0]]
            # if the resulting minimum distance is >= the results minimum 
            # distance, you may replace it. 
            if c_min_dist >= np.min(delf.dists[idx]):
                break
        # replace the idx
        rep_score = self.results[idx].score
        _log.debug('%s replacing %s'%(self.results[idx], res))
        self.results[idx] = res
        self._update_dists(idx)
        if rep_score == self.min:
            self._update_min()
        return True

    def _update_min(self):
        '''
        Updates current minimum score.
        '''
        new_min = np.inf
        for res in self.results:
            if res.score < new_min:
                new_min = res.score
        _log.debug('New minimum score is %.3f'%(new_min))
        self.min = new_min

    def get_results(self):
        '''
        Returns the results in sorted order, sorted by score. Returns them
        as (image, score, frameno)
        '''
        _log.debug('Dumping results')
        sco_by_idx = np.argsort([x.score for x in self.results])
        res = []
        for idx in sco_by_idx:
            res_obj = self.results[idx]
            if not res_obj._defined:
                continue
            res.append(res_obj.image, res_obj.score, res_obj.frameno)
        return res

class LocalSearcher(object):
    def __init__(self, predictor, face_finder,
                 eye_classifier, 
                 processing_time_ratio=1.0,
                 local_search_width=32,
                 local_search_step=4,
                 n_thumbs=5,
                 mixing_samples=10,
                 search_algo=MCMH_rpl,
                 max_variety=True,
                 feature_generators=None,
                 feats_to_cache=None,
                 combiner=None,
                 filters=None):
        '''
        Inputs: 
            predictor:
                computes the score for a given image
            local_search_width:
                The number of frames to search forward.
            local_search_step:
                The step size between adjacent frames.
                ===> for instance, if local_search_width = 6 and
                     local_search_step = 2, then it will obtain 6 frames
                     across 12 frames (about 0.5 sec) 
            n_thumbs:
                The number of top images to store.
            mixing_samples:
                The number of samples to draw to establish baseline
                statistics.
            search_algo:
                Selects the thumbnails to try; accepts the number of elements
                over which to search. Should support asynchronous result 
                updating, so it is easy to switch the predictor between
                sequential (CPU-based) and non-sequential (GPU-based)
                predictor methods. Further, it must be able to accept an
                indication that the frame search request was BAD (i.e., it
                couldn't be read).
            max_variety:
                If True, the local searcher will maximize the variety of the
                images.
            feature_generators:
                A list of feature generators. Note that this have to be of the
                RegionFeatureGenerator type. The features (those that are not
                required by the filter, that is) are extracted in the order
                specified. This is due to some of the features requiring
                sequential processing. Thus, an ordered dict is used.
            feats_to_cache:
                The name of all features to save as running statistics.
                (features are only cached during sampling)
            combiner:
                Combines the feature scores. See class definition above. This
                replaces the notion of a list of criteria objects, which
                proved too abstract to implement.
            filters:
                A list of filters which accept feature vectors and return 
                which frames need to be filtered. Each filter should surface
                the name of the feature generator whose vector is to be used,
                via an attribute that is simply named "feature."
                Filters are applied in-order, and only non-filtered frames
                have their features extracted per-filter. 
        '''
        self.predictor = predictor
        self.processing_time_ratio = processing_time_ratio
        self.local_search_width = local_search_width
        self.local_search_step = local_search_step
        self.n_thumbs = n_thumbs
        self.mixing_samples = mixing_samples
        self.search_algo = search_algo(local_search_width)
        self.generators = odict()
        self.feats_to_cache = odict()
        self.combiner = combiner
        self.filters = filters
        self.cur_frame = None
        self.video = None
        self.video_name = None
        self.results = ResultsList(self.n_thumbs)
        self.stats = dict()
        self.fps = 0

        # determine the generators to cache.
        for f in feature_generators:
            gen_name = f.get_feat_name()
            self.generators[gen_name] = f
            if gen_name in feats_to_cache:
                self.feats_to_cache[gen_name] = f

    @property
    def min_score(self):
        return self.results.min

    @min_score.setter
    def min_score(self, value):
        pass # cannot be set, not sure if it's required

    @min_score.deleter
    def min_score(self):
        pass # cannot be deleted

    def choose_thumbnails(self, video, n=1, video_name=''):
        thumbs = self.choose_thumbnails_impl(video, n, video_name)
        return thumbs

    def choose_thumbnails_impl(self, video, n=1, video_name=''):
        # instantiate the statistics objects required
        # for computing the running stats.
        for gen_name in self.feats_to_cache_name:
            self.stats[gen_name] = Statistics()
        self.stats['score'] = Statistics()

        self.results.reset()
        self.min_score = 0
        # maintain results as:
        # (score, rtuple, frameno, colorHist)
        #
        # where rtuple is the value to be returned.
        self.video = video
        self.video_name = video_name
        fps = video.get(cv2.cv.CV_CAP_PROP_FPS) or 30.0
        num_frames = int(video.get(cv2.cv.CV_CAP_PROP_FRAME_COUNT))
        video_time = float(num_frames) / fps
        self.search_algo.start(num_frames)
        start_time = time()
        max_processing_time = self.processing_time_ratio * video_time
        _log.info('Starting search of video with %i frames, for %s seconds'%(
                        num_frames, max_processing_time))
        while (time() - start_time) < max_processing_time:
            r = self._step()
            if r == False:
                _log.info('Searched whole video')
                # you've searched as much as possible
                break
        raw_results = self.results.get_results()
        # format it into the expected format
        results = []
        for rr in raw_results:
            formatted_result = (rr[0], rr[1], rr[2], rr[2] / float(fps),
                                '')
            results.append(formatted_result)
        return results

    def _conduct_local_search(self, start_frame, end_frame, 
                              start_score, end_score):
        '''
        Given the frames that are already the best, determine whether it makes
        sense to proceed with local search. 
        '''
        _log.debug('Local search of %i [%.3f] <---> %i [%.3f]'%(
                    start_frame, start_score, end_frame, end_score))
        if not self._should_search(start_score, end_score):
            return
        frames, framenos = self.get_search_frame(start_frame)
        if frames == None:
            # uh-oh, something went wrong! In this case, the search region
            # will not be searched again, and so we don't have to worry about
            # updating the knowledge of the search algo.
            return
        frame_feats = dict()
        allowed_frames = np.ones(len(frames)).astype(bool)
        # obtain the features required for the filter.
        for f in self.filters:
            fgen = self.generators[f.feature]
            feats = fgen.generate(frames)
            frame_feats[f.feature] = feats
            accepted = f.filter(feats)
            _log.debug(('Filter for feature %s has rejected %i',
                        ' frames, %i remain')%(f.feature, 
                                                np.sum(
                                                    np.logical_not(accepted)),
                                                np.sum(accepted)))
            if not np.any(accepted):
                _log.debug('No frames accepted by filters')
                return
            # filter the current features across all feature
            # dicts, as well as the framenos
            for k in frame_feats.keys():
                frame_feats[k] = [x for n, x in enumerate(frame_feats[k])
                                    if accepted[n]]
            framenos = [x for n, x in enumerate(framenos) if accepted[n]]
            frames = [x for n, x in enumerate(frames) if accepted[n]]
        for k, f in self.generators.iteritems():
            if k in frame_feats:
                continue
            frame_feats[k] = f.generate(frames)
        # get the combined scores
        comb = self.combine(frame_feats.keys(), frame_feats.values())
        comb = np.array(comb)
        best_frameno = framenos[np.argmax(comb)]
        best_frame = frames[np.argmax(comb)]
        _log.debug(('Best frame from interval %i [%.3f] <---> %i [%.3f]',
                    ' is %i with feature score %.3f')%(start_frame, 
                            start_score, end_frame, end_score, best_frameno,
                            np.max(comb)))
        # the selected frame (whatever it may be) will be assigned
        # the score equal to mean of its boundary frames. 
        framescore = (start_score + end_score) / 2
        # push the frame into the results object.
        self.results.accept_replace(best_frameno, framescore, best_frame,
                                    np.max(comb))

    def _take_sample(self, frameno):
        '''
        Takes a sample, updating the estimates of mean score, mean image
        variance, mean frame xdiff, etc.
        '''
        frames = self.get_seq_frames(self.video,
                    [frameno, frameno + self.local_search_step])
        if frames == None:
            # uh-oh, something went wrong! Update the knowledge state of the
            # search algo with the knowledge that the frame is bad.
            self.search_algo.update(frameno, bad=True)
            return
        # get the score the image.
        frame_score = self.predictor.predict(frames[0])
        # extract all the features we want to cache
        for n, f in zip(self.feats_to_cache_name, 
                        self.feats_to_cache):
            vals = f.generate(frames, fonly=True)
            self.stats[n].push(vals[0])
        self.stats['score'].push(frame_score)
        _log.debug('Took sample at %i, score is %.3f'%(frameno, frame_score))
        # update the search algo's knowledge
        self.search_algo.update(frameno, frame_score)

    def _should_search(self, start_score, end_score):
        '''
        Accepts a start frame score and the end frame score and returns True /
        False indicating if this region should be searched.

        Regions are searched if and only if:
            - the mean of their score exceeds the min of the results.
            - mean of their scores exceeds the mean score observed so far.
        '''
        mean_score = (start_score + end_score) / 2.
        if mean_score > self.min_score:
            if mean_score > self.stats['score'].mean():
                _log.debug('Interval should be searched')
                return True
            else:
                _log.debug('Interval can be in results but does not exceed',
                           ' mean observed score data')
        _log.debug('Interval cannot be admitted to results')
        return False
        
    def _step(self):
        r = self.search_algo.get()
        if r == None:
            return False
        action, meta = r
        if action == 'sample':
            self._take_sample(meta)
        else:
            self._conduct_local_search(*meta)
        return True

    def _update_color_stats(self, images):
        '''
        Computes a color similarities for all pairwise combinations of images.
        '''
        colorObjs = [ColorName(img) for img in images]
        dists = []
        for i, j in permutations(range(len(images))):
            dists.append(i.dist(j))
        self._tot_colorname_val[0] = np.sum(dists)
        self._tot_colorname_val[1] = len(dists)
        self._colorname_stat = (self._tot_colorname_val[0] * 1./
                                self._tot_colorname_val[1])
            
    def _mix(self):
        '''
        'mix' takes a number of equispaced samples from the video. This is
        inspired from the notion of mixing for a Markov chain. 
        '''
        _log.info('Mixing before search begins for %i frames'%(
                                                    self.mixing_samples))
        num_frames = self.mixing_samples
        samples = np.linspace(0, num_frames, 
                              self.mixing_samples+2).astype(int)
        samples = [self.search_algo.get_nearest(x) for x in samples]
        samples = list(np.unique(samples))
        # we need to be able to compute the SAD, so we need to
        # also insert local search steps
        for frameno in samples:
            self._take_sample(framemno)

    def _get_frame(self, f):
        try:
            more_data, self.cur_frame = pycvutils.seek_video(
                                        self.video, f, 
                                        cur_frame=self.cur_frame)
            if not more_data:
                if self.cur_frame is None:
                    raise model.errors.VideoReadError(
                        "Could not read the video")
            more_data, frame = self.video.read() 
        except model.errors.VideoReadError:
            statemon.state.increment('cv_video_read_error')
            frame = None
        except Exception as e:
            _log.exception("Unexpected error when searching through video %s" %
                           self.video_name)
            statemon.state.increment('video_processing_error')
            frame = None
        return frame

    def get_seq_frames(self, framenos):
        '''
        Acquires a series of frames, in sorted order.

        NOTE: This does not ensure that you will not seek off the video. It is
        up to the caller to ensure this is the case.
        '''
        if not type(framenos) == list:
            framenos = [framenos]
        frames = []
        for frameno in framenos:
            frame = self._get_frame(frameno)
            if frame == None:
                return None
            frames.append(frame)
        return frames

    def get_region_frames(self, start, num=1,
                          step=1):
        '''
        Obtains a region from the video.
        '''
        frame_idxs = [start]
        for i in range(num-1):
            frame_idxs.append(frame_idxs[-1]+step)
        frames = get_seq_frames(framenos)
        return frames

    def get_search_frame(self, start_frame):
        '''
        Obtains a search region from the video. 
        '''
        num = (self.local_search_width /
               self.local_search_step)
        frames = self.get_region_frames(self,
                start_frame, num,
                self.local_search_step)
        frameno = range(start_frame, 
                        start_frame + self.local_search_width + 1,
                        self.local_search_step)
        return frames, frameno