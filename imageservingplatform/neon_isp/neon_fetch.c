// Fetch the document from S3 using wget.
// TODO: The call to be sandboxed in a process, so that on crash it doesn't affect the current proc/ thread

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
           time_t timeout)
{
    static char command[1024];
    static const char * const format = "wget --quiet --timeout=%d --output-document=%s %s";
    int ret = 0;
    
    neon_log_error("neon_fetch");
    sprintf(command, format, timeout, mastermind_filepath, mastermind_url);
    
    errno = 0;
    ret = system(command);
    
    if(ret == 0)
        return NEON_FETCH_OK;
    
    if(ret == -1) {
        neon_fetch_error = strerror(errno);
        return NEON_FETCH_FAIL;
    }
    
    return NEON_FETCH_FAIL;
}


