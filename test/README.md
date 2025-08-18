To run the pytest suite you need to do the following tasks

- Install Debian dependencies (apt version of docker is too old)
  sudo apt-get install zip python3.9 python3.9-dev libcairo2-dev pkg-config krb5-user libssl-dev libcairo2-dev libgirepository1.0-dev libgirepository1.0-dev
  sudo snap install docker

- Install requirements.txt from src/backend
    python3.9 -m pip install -r requirements.txt

- Install pytest and test deps
    python3.9 -m pip install pytest
    python3.9 -m pip install sarge
    python3.9 -m pip install pytest-cov
    python3.9 -m pip install testfixtures

- Build edgemlsdk artifact - go to /src/edgemlsdk/build.sh

- run build-custom.sh to build all the deps
    cd /src/backend/edmlsdk/panorama-1.0-py3-none-any.whl
    python3.9 -m pip install 

- install the artifacts from the build

- Set python path to include edgemlSDK binaries
export PYTHONPATH=$PYTHONPATH:/home/<username>/<checkoutdir>/src/backend:/home/ryvan/<username>/<checkoutdir>/edgemlsdk/src/src/bindings/python/python_package/src/

- run the tests!
- cd test
    python3.9 -m pytest -v
