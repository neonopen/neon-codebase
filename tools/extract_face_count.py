#!/usr/bin/env python
'''
Script that counts the number of faces in each image

Takes as input a text file with one filename per line.

Outputs a pandas pickle where each column is a file and each row is a feature vector value.

Author: Mark Desnoyer (desnoyer@neon-lab.com)
Copyright 2016 Neon Labs Inc.
'''

import os
import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

import concurrent.futures
import cv2
import dlib
import logging
import hashlib
import pandas as pd
import skimage.io
import re
import utils.neon
from utils.options import options, define

_log = logging.getLogger(__name__)
define('input', default=None,
       help='Input file with image filenames, one per line')
define('output', default=None,
       help='Output file, which will be a pandas pickle')
define('cache_dir', default=None,
       help='Directory to store cached results')
define('prefix', default=None, type=str,
       help='Prefix to remove from the filename in the index')
define('n_workers', default=4, 
       help='Number of parallel workers to use')

def _get_features_impl(filename, detector):
    _log.debug('Opening %s' % filename)
    image = skimage.io.imread(filename)
    dets, scores, idx = detector.run(image, 1)

    name = filename
    if options.prefix:
        name = re.sub(options.prefix, '', filename)

    return pd.Series({'face_count': len(dets)}, name=name)

def get_features(filename, detector):
    cache_file = 'faces_%s.pkl' % hashlib.md5(filename).hexdigest()
    if options.cache_dir is not None:
        full_cache_fn = os.path.join(options.cache_dir, cache_file)
        if os.path.exists(full_cache_fn):
            data = pd.read_pickle(full_cache_fn)
            return data

    data = _get_features_impl(filename, detector)

    if options.cache_dir is not None:
        full_cache_fn = os.path.join(options.cache_dir, cache_file)
        if not os.path.exists(options.cache_dir):
            os.makedirs(options.cache_dir)
        data.to_pickle(full_cache_fn)

    return data

def image_file_iterator():
    _log.info('Reading image files from %s' % options.input)
    with open(options.input, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                yield line

def main():
    detector = dlib.get_frontal_face_detector()
    success_count = 0
    fail_count = 0
    vecs = []
    with concurrent.futures.ProcessPoolExecutor(options.n_workers) as executor:
        for fut in concurrent.futures.as_completed(
                [executor.submit(get_features, image_file, detector)
                 for image_file in image_file_iterator()]):
            try:
                vecs.append(fut.result())
                success_count += 1
                if success_count % 100 == 0:
                    _log.info('Processed %i successfully, %i failed' %
                              (success_count, fail_count))
            except Exception as e:
                _log.error('Error processing image %s' % e)
                fail_count += 1


    df = pd.concat(vecs, axis=1)
    df.to_pickle(options.output)
    _log.info('Output file to %s' % options.output)

if __name__ == '__main__':
    utils.neon.InitNeon()
    logging.getLogger('boto').propagate = False
    main()
