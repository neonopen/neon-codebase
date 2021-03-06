cmake_minimum_required(VERSION 2.8)
PROJECT(imageservingplatform)

include(${CMAKE_SOURCE_DIR}/Common.cmake)

include_directories(${imageservingplatform_SOURCE_DIR})
include_directories(${imageservingplatform_SOURCE_DIR}/rapidJson)
include_directories(${imageservingplatform_SOURCE_DIR}/../nginx-1.8.1/objs)
include_directories(${imageservingplatform_SOURCE_DIR}/../nginx-1.8.1/src/core)
include_directories(${imageservingplatform_SOURCE_DIR}/../nginx-1.8.1/src/event)
include_directories(${imageservingplatform_SOURCE_DIR}/../nginx-1.8.1/src/http)
include_directories(${imageservingplatform_SOURCE_DIR}/../nginx-1.8.1/src/mail)
include_directories(${imageservingplatform_SOURCE_DIR}/../nginx-1.8.1/src/misc)
include_directories(${imageservingplatform_SOURCE_DIR}/../nginx-1.8.1/src/http/modules)
include_directories(${imageservingplatform_SOURCE_DIR}/../nginx-1.8.1/src/os/unix)

add_library(NeonISP STATIC
 neon_mastermind.cpp
 neon_updater.cpp
 neon_utils.c
 neon_utc.c
 neon_log.c
 mastermind.cpp
 publisherHashtable.cpp
 neon_log.c 
 directiveHashtable.cpp 
 directive.cpp 
 publisher.cpp 
 fraction.cpp 
 scaledImage.cpp 
 defaultThumbnail.cpp
 defaultThumbnailHashtable.cpp
 neonException.cpp 
 neon_error_codes.c
 neon_stats.c
 neon_service_helper.c
 url_utils.cpp
)

######## All tests after this point #########

add_cc_test(NeonISPTest
  test/neon_mastermind_test.cc
  test/neon_utils_test.cc
  test/rapidjson_test.cc
  test/neon_publisherhashtable_test.cc
  test/neon_directivehashtable_test.cc
  test/neon_service_helper_test.cc
  test/fractions_test.cc
  test/default_thumbnail_test.cc
  test/test_utils.cc
)

target_link_libraries( NeonISPTest 
    NeonISP
)

### python tests ###
enable_python()
