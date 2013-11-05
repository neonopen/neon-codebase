'''
Stuff to set up the Neon environment.

In your __main__ routine, run InitNeon() and everything will be
magically setup.

Author: Mark Desnoyer (desnoyer@neon-lab.com)
Copyright 2013 Neon Labs
'''

import logging

from . import logs
from . import options

def InitNeon():
    '''Perform the initialization for the Neon environment.

    Returns the leftover arguments
    '''
    garb, args = options.parse_options()
    logs.AddConfiguredLogger()

    return args

def InitNeonTest():
    '''Perform the initialization for the Neon unittest environment.

    In particular, this silences all the logs.
    '''
    options.parse_options()

    # Remove all the loggers that some sub libraries may have setup
    for mod, logger in logging.Logger.manager.loggerDict.iteritems():
        logger.handlers = []

    # Make a new logger that dumps stuff to /dev/null in case some sub
    # library tries to re-enable logging.
    logs.CreateLogger(logfile='/dev/null')

    logging.captureWarnings(True)
