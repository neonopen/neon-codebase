#!/usr/bin/env python
'''
Unittests for the sanitizer module
'''
import os.path
import sys
sys.path.insert(0,os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

import unittest
from utils.inputsanitizer import InputSanitizer

class TestInputSantizer(unittest.TestCase):

    def setUp(self):
        pass

    def test_bool_false(self):

        input_list = ['false','False','FALSE','FalSE',False]
       
        for input in input_list:
            self.assertEqual(InputSanitizer.to_bool(input),False)
    
    def test_bool_True(self):

        input_list = ['true','True','TRUE',True] 
       
        for input in input_list:
            self.assertEqual(InputSanitizer.to_bool(input),True)

    def test_to_string(self):
        s = 'teststring'
        self.assertEqual(InputSanitizer.to_string(s),s)
        
        l = [ "s","t","r" ]
        self.assertEqual(InputSanitizer.to_string(l),"str")

    def test_valid_http_url(self):
        #TO BE FIXED 
        links = ["http://image.jpg"] 
        for link in links:
            self.assertIsNotNone(InputSanitizer.validate_http_url(link))
        
        invs = ["htt://agag.jpg", "htttp://images.jpg", "soemrangomstring"]
        for inv in invs:
            self.assertIsNone(InputSanitizer.validate_http_url(inv))

if __name__ == '__main__':
    unittest.main()
