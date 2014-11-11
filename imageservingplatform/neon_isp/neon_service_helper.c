/*
 * Static helper functions for use by neon_service
 * 
 * */

#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_http.h>

/*
 * Header Search Function (Used to get custom headers)
 *
 * */
static ngx_table_elt_t *
search_headers_in(ngx_http_request_t *r, u_char *name, size_t len) {
    ngx_list_part_t            *part;
    ngx_table_elt_t            *h;
    ngx_uint_t                  i;
 
    /*
    Get the first part of the list. There is usual only one part.
    */
    part = &r->headers_in.headers.part;
    h = (ngx_table_elt_t *) part->elts;
 
    /*
    Headers list array may consist of more than one part,
    so loop through all of it
    */
    for (i = 0; /* void */ ; i++) {
        if (i >= part->nelts) {
            if (part->next == NULL) {
                /* The last part, search is done. */
                break;
            }
 
            part = part->next;
            h = (ngx_table_elt_t *) part->elts;
            i = 0;
        }
 
        /*
        Just compare the lengths and then the names case insensitively.
        */
        if (len != h[i].key.len || strcasecmp((const char*)name, (const char*)h[i].key.data) != 0) {
            /* This header doesn't match. */
            continue;
        }
 
        /*
        Ta-da, we got one!
        Note, we'v stop the search at the first matched header
        while more then one header may fit.
        */
        return &h[i];
    }
 
    /*
    No headers was found
    */
    return NULL;
}

/*
 * Parse a ngx_str value in to long 
 *
 * */
static long neon_service_parse_number(ngx_str_t * value){

    int base = 10;
    static const int bufferSize = 16;
    char buffer[bufferSize];
    char *endptr = 0;
    long val;

    memset(buffer, 0, 16);
    
    // must be smaller than buffer + terminating zero
    if((int)value->len > (bufferSize-1))
        return -1;
    
    strncpy(buffer, (char*)value->data, (size_t)value->len);
    
    errno = 0;
    val = strtol(buffer, &endptr, base);
    
    if ((errno == ERANGE && (val == LONG_MAX || val == LONG_MIN))
        || (errno != 0 && val == 0)) {
        return -1;
    }
    
    if (endptr == buffer) {
        // no digits were found
        return -1;
    }
    
    return val;
}



