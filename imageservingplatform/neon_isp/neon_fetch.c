/*
 * Download the mastermind file from S3 via a custom s3downloader script
 *
 */

#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include "neon_log.h"
#include "neon_stats.h"
#include "neon_fetch.h"

const char * neon_fetch_error = 0;

NEON_FETCH_ERROR
neon_fetch(const char * const mastermind_url,
           const char * const mastermind_filepath,
           const char * const s3port,
           const char * const s3downloader_path,
           time_t timeout)
{
    static char command[1024];
    int ret = 0;
    
    static const char * format = "/usr/local/bin/isp_s3downloader -u %s -d %s";
    if (s3port != NULL && s3downloader_path != NULL){
        format = "%s -u %s -d %s -s localhost -p %s";
        if (sprintf(command, format, s3downloader_path, mastermind_url, mastermind_filepath, s3port) <= 0){
            neon_log_error("sprintf failed to print the command");
            return NEON_FETCH_FAIL;
        }
        
    }else{
        sprintf(command, format, mastermind_url, mastermind_filepath); 
    }
    
    errno = 0;
       
    neon_log_error("s3 download cmd %s", command);
    ret = system(command);
    
    if (ret == 0)
        return NEON_FETCH_OK;
    
    if (ret == -1) {
        neon_fetch_error = strerror(errno);
        return NEON_FETCH_FAIL;
    }
    
    return NEON_FETCH_FAIL;
}


