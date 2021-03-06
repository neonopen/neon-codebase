#ifndef _NEON_FETCH_
#define _NEON_FETCH_

#ifdef __cplusplus
extern "C" {
#endif

#include <time.h>
#include "neon_error_codes.h"


typedef enum  {
    
    NEON_FETCH_OK = 0,
    NEON_FETCH_FAIL
    
} NEON_FETCH_ERROR;


extern const char * neon_fetch_error;

/*
 *
 */
NEON_FETCH_ERROR neon_fetch(const char * const mastermind_url,
                            const char * const mastermind_filepath,
                            const char * const s3cmd_conf_fpath,
                            const char * const s3downloader_fpath,
                            time_t timeout, 
                            char ** script_output);



#ifdef __cplusplus
}
#endif
#endif

