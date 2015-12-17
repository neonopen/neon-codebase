#!/usr/bin/env python
'''
    Script responsible for moving everything in our Redis store to 
       Postgres on Amazon RDS  
''' 
import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

from cmsdb import neondata
from contextlib import closing
import logging
import psycopg2
import utils
import utils.neon
from utils.options import define, options

_log = logging.getLogger(__name__)

def move_abstract_integrations(): 
    ''' 
         moves all the Integrations, saves them as 
         abstractintegration in postgres 
    '''  
    integrations = neondata.AbstractIntegration.get_all()
    options._set('cmsdb.neondata.wants_postgres', 1)
    for i in integrations: 
        try: 
            i.save()
        except Exception as e: 
            _log.exception('Error saving integration %s to postgres %s' % (i,e))  
            pass 

def move_abstract_platforms(): 
    ''' 
        moves all the platforms, saves them as the platform class 
        name in postgres 
    '''  
    platforms = neondata.AbstractPlatform.get_all()
    options._set('cmsdb.neondata.wants_postgres', 1)
    for p in platforms: 
        try: 
            p.modify(p.key, p.integration_id, lambda x: x, create_missing=True)
        except Exception as e:
            _log.exception('Error saving platform %s to postgres %s' % (p,e)) 
            pass  

def move_experiment_strategies(): 
    ''' 
        moves all the ExperimentStrategies
    '''  
    strategies = neondata.ExperimentStrategy.get_all()
    options._set('cmsdb.neondata.wants_postgres', 1)
    for s in strategies: 
        try: 
            s.save()
        except Exception as e:
            _log.exception('Error saving exp stratgey %s to postgres %s' % (p,e)) 
            pass  

def move_neon_user_accounts():
    ''' 
        moves all the NeonUserAccounts, NeonApiKeys
    '''  
    accts = neondata.NeonUserAccount.get_all()
    options._set('cmsdb.neondata.wants_postgres', 1) 
    for acct in accts:
        try:
            #import pdb; pdb.set_trace() 
            acct.save() 
            api_key = neondata.NeonApiKey(acct.account_id, 
                                          acct.neon_api_key) 
            api_key.save() 
        except Exception as e: 
            _log.exception('Error saving account %s to postgres %s' % (acct,e))
            pass  

def move_cdn_hosting_metadata_lists(): 
    ''' 
        moves all the CDNHostingMetadataLists 
    '''  
    cdns = neondata.CDNHostingMetadataList.get_all() 
    options._set('cmsdb.neondata.wants_postgres', 1)
    for c in cdns: 
        try: 
            c.save() 
        except Exception as e:
            _log.exception('Error saving cdnhostingmetadatalist %s to postgres %s' % (p,e)) 
            pass  
 
def move_neon_videos_and_thumbnails():
    ''' 
        move VideoMetadata, ThumbnailMetadata, ThumbnailStatus 
             VideoStatus
    '''  
    accts = neondata.NeonUserAccount.get_all()
    for acct in accts:
        #print acct 
        #import pdb; pdb.set_trace()
        #videos = list(acct.iterate_all_videos())
        for v in acct.iterate_all_videos():
            options._set('cmsdb.neondata.wants_postgres', 0)
            try:
                tnails = neondata.ThumbnailMetadata.get_many(v.thumbnail_ids)
                if v.job_id: 
                    api_request = neondata.NeonApiRequest.get(v.job_id, acct.neon_api_key)
                tnail_statuses = neondata.ThumbnailStatus.get_many(v.thumbnail_ids)
                tnail_serving_urls = neondata.ThumbnailServingURLs.get_many(v.thumbnail_ids) 
                video_status = neondata.VideoStatus.get(v.key)  
                options._set('cmsdb.neondata.wants_postgres', 1) 
                for t in tnails:
                    try:  
                        if t: 
                            t.save() 
                    except Exception as e: 
                        _log.exception('Error saving thumbnail %s to postgres %s' % (t,e)) 
                        pass 
                for ts in tnail_statuses:
                    try: 
                        if ts:  
                            ts.save() 
                    except Exception as e: 
                        _log.exception('Error saving thumbnail_status %s to postgres %s' % (ts,e)) 
                        pass 
                for tsu in tnail_serving_urls:
                    try: 
                        if tsu:  
                            tsu.save() 
                    except Exception as e:
                        _log.exception('Error saving thumbnail_serving_url %s to postgres %s' % (tsu,e)) 
                        pass
                try:  
                    video_status.save() 
                except Exception as e: 
                    _log.exception('Error saving video_status %s to postgres %s' % (video_status,e)) 
                    pass
                
                try: 
                    v.save()
                except Exception as e:
                    _log.exception('Error saving video %s to postgres %s' % (v,e)) 
                    pass

                try: 
                    api_request.save() 
                except Exception as e: 
                    _log.exception('Error saving api_request %s to postgres %s' % (api_request,e)) 
                    pass

                options._set('cmsdb.neondata.wants_postgres', 0)
            except Exception as e: 
                _log.exception('Error pulling information for video %s while saving to postgres %s' % (v,e)) 
                pass

def main():
    #move_neon_user_accounts()
    move_neon_videos_and_thumbnails()
    #move_abstract_integrations()
    #move_abstract_platforms()
    #move_cdn_hosting_metadata_lists()
    #move_experiment_strategies()

if __name__ == "__main__":
    utils.neon.InitNeon()
    main()
