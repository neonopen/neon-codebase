#include "neon_stats.h"
#include "publisherHashtable.h"

/*
 *   Publisher Table
 */

PublisherHashtable::PublisherHashtable()
{
    table = 0;
    initialized = false;
}

PublisherHashtable::~PublisherHashtable()
{
    table = 0;
    initialized = false;
}

void
PublisherHashtable::Init(unsigned numOfBuckets)
{
    if(initialized == true) {
        neon_stats[NEON_PUBLISHER_HASTABLE_INVALID_INIT]++;
        return;
    }

    table = new PublisherTable(numOfBuckets);
    
    initialized = true;
}

void
PublisherHashtable::Shutdown()
{
    if(initialized == false) {
        neon_stats[NEON_PUBLISHER_HASTABLE_INVALID_SHUTDOWN]++;
        return;
    }
    
    for(PublisherTable::iterator it = table->begin(); it != table->end(); it ++)
    {
        Publisher * p = (*it).second;
        (*it).second = 0;

        if(p == NULL) {
            neon_stats[NEON_PUBLISHER_SHUTDOWN_NULL_POINTER]++;
            continue;
        }

        p->Shutdown();
        delete p;
    }
    
    delete table;
	table = 0;
    initialized = false;
}

unsigned
PublisherHashtable::GetSize()
{
    return table->size();
}

void
PublisherHashtable::AddPublisher(rapidjson::Document & document)
{
    
    Publisher * p = new Publisher();
    
    // get publisher id
    if(document.HasMember("pid") == false)
    	return;
    	
    const char * publisherId = document["pid"].GetString();
    
    // get account id
    if(document.HasMember("aid") == false)
    	return;
    	
    const char * accountId = document["aid"].GetString();
    
    int ret = p->Init(publisherId, accountId);
    
    if(ret != 0)
    	return;
    
    std::string key = publisherId;
    
    (*table)[key] = p;
	return;
}

Publisher *
PublisherHashtable::Find(const char * publisherIdKey)
{
    std::string key = publisherIdKey;
   
    Publisher * publisher = (*table)[key];
    
    if(publisher == 0)
        return 0;
    
    return publisher;
}
