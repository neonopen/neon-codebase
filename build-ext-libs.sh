
#!/bin/bash
# -----------------------------------------------------------
# Build the local Neon External Libraries: PProf & libunwind
# -----------------------------------------------------------
# https://sites.google.com/a/neon-lab.com/engineering/system-setup/dependencies
#
set -e
dir=$(dirname $0)

case $(uname -s) in                                                                                                                                                                    ( Darwin )
    echo "ERROR: OSX is not supported." 1>&2
    exit 1
    ;;
  ( Linux )
    # Presumed to be Ubuntu
    lsb_rel=$(lsb_release --short --release)
    printf "Ubuntu $lsb_rel "
    case $lsb_rel in
      ( 12.04 ) #
        echo "is supported."
        ;;
      ( * )
        echo "is UNTESTED." 
        ;;
    esac

    # Python 2.7
    sudo apt-get install \
      python-dev \
      python-pip
    sudo pip install "virtualenv>1.11.1"

    # GCC 4.6
    # GFortran
    # CMake > 2.8
    sudo apt-get install \
      build-essential \
      gfortran \
      cmake

    # Libraries
    # https://sites.google.com/a/neon-lab.com/engineering/system-setup/dependencies#TOC-Libraries
    sudo apt-get install \
      libatlas-base-dev \
      libyaml-0-2 \
      libmysqlclient-dev \
      libboost1.46-dbg \
      libboost1.46-dev \
      libfreetype6-dev \
      libcurl4-openssl-dev \
      libjpeg-dev

    sudo ln -s /usr/lib/x86_64-linux-gnu/libjpeg.so /usr/lib
    sudo ln -s /usr/lib/x86_64-linux-gnu/libfreetype.so /usr/lib
    sudo ln -s /usr/lib/x86_64-linux-gnu/libz.so /usr/lib

    cd $dir/externalLibs
    # libunwind 
    printf "Checking libunwind: "
    if readlink -e /usr/local/lib/libunwind-x86_64.so ; then
      echo "libunwind installed"
    else
      tar -xzf libunwind-0.99-beta.tar.gz
      cd libunwind-0.99-beta
      ./configure CFLAGS=-U_FORTIFY_SOURCE LDFLAGS=-L`pwd`/src/.libs
      sudo make install
      cd ..
    fi

    # GPerfTools
    printf "Checking gperftools: "
    if readlink -e /usr/local/lib/libtcmalloc_minimal.so ; then
       echo "gperftools installed"
    else
      tar -xzf gperftools-2.1.tar.gz
      cd gperftools-2.1
      ./configure
      sudo make install
      cd ..
    fi

    # https://sites.google.com/a/neon-lab.com/engineering/system-setup/dependencies#TOC-Fast-Fourier-Transform-Package-FFTW3-
    sudo apt-get install fftw3-dev

    # GFlags
    ./install_gflags.sh

    # MySQL - https://sites.google.com/a/neon-lab.com/engineering/system-setup/dependencies#TOC-MySql
    sudo apt-get install mysql-client

    # Redis - https://sites.google.com/a/neon-lab.com/engineering/system-setup/dependencies#TOC-Redis
    sudo apt-get install redis-server

    # PCRE Perl lib (required for http rewrite module of nginx)
    #apt-get install libpcre3 libpcre3-dev

    # Ruby
    ./install_ruby.sh

    # Hadoop
    ./install_hadoop.sh

    # OpenCV
    ./install_opencv.sh
    ;;
esac

# vim: set ts=2 sw=2 sts=2 expandtab #
