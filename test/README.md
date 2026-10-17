To run the pytest suite you need to do the following tasks

- Install Debian dependencies (apt version of docker is too old)
  sudo apt-get install zip python3.11 python3.11-dev libcairo2-dev pkg-config krb5-user libssl-dev libcairo2-dev libgirepository1.0-dev libgirepository1.0-dev
  sudo snap install docker

- Install requirements.txt from src/backend
    python3.11 -m pip install -r requirements.txt

- Install pytest and test deps
    python3.11 -m pip install pytest
    python3.11 -m pip install sarge
    python3.11 -m pip install pytest-cov
    python3.11 -m pip install testfixtures

- Build edgemlsdk artifact - go to /src/edgemlsdk/build.sh

- run build-custom.sh to build all the deps
    cd /src/backend/edmlsdk/panorama-1.0-py3-none-any.whl
    python3.11 -m pip install 

- install the artifacts from the build

- Set python path to include edgemlSDK binaries
export PYTHONPATH=$PYTHONPATH:/home/<username>/<checkoutdir>/src/backend:/home/ryvan/<username>/<checkoutdir>/edgemlsdk/src/src/bindings/python/python_package/src/

- run the tests!
- cd test
    python3.11 -m pytest -v

## Running the backend tests inside the flask-app image

The backend tests import the full backend (edgemlsdk native bindings,
GStreamer, `libtritonserver.so`), so they run only where those dependencies
exist: inside the built flask-app image, not in a bare checkout.

There are two ways to run them:

1. As part of the build. `build-custom.sh` runs the backend test suites in the
   freshly built flask-app image and fails the build if they fail. Watch for
   `Backend unit tests passed.` Set `SKIP_BACKEND_TESTS=1` to skip them (for
   example on an offline host, because the step pip-installs the test
   dependencies).
2. By hand, against an existing flask-app image, from the repository root. Use
   the image's backend interpreter: `python3.11`, or `python3.10` for a JP6
   image.

```bash
docker run --rm -v "$PWD":/repo -w /repo --entrypoint bash flask-app -c '
  python3.11 -m pip install --no-cache-dir --quiet pytest pytest-cov sarge testfixtures hypothesis
  export PYTHONPATH=/repo/src/backend
  export LD_LIBRARY_PATH=/opt/tritonserver/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}
  python3.11 -m pytest test/backend-test/... -v
'
```

Notes for the in-image run:

- The working directory must be the repository root (`/repo`). `conftest.py`
  and the test helpers build paths from `os.getcwd()`.
- `LD_LIBRARY_PATH` must include `/opt/tritonserver/lib`, or collection fails
  with `ImportError: libtritonserver.so: cannot open shared object file`.
- For coverage, add for example `--cov=src/backend --cov-report=term-missing`.
