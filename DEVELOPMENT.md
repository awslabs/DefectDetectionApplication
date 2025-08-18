# EdgeMLDefectDetectionLocalServer



## Install Greengrass
Notes:
1. Greengrass should be installed at this location `/aws_dda/greengrass/v2` as that's the docker mount location

## Building the component
1. Install GDK by following instructions - https://docs.aws.amazon.com/greengrass/v2/developerguide/install-greengrass-development-kit-cli.html
2. Install Docker and Docker-compose
3. To build the component - `gdk component build`
4. To publish the component
   i. Export credentials of the AWS account in which you want to publish the component  
   ii. Update `gdk-config.json` to set correct region   
   iii. `gdk component publish`

## Unit Testing
1. In PyCharm, mark the src/backend as source and test/backend as test directory
2. In PyCharm, click on Tools > Sync Python Requirements > select the requirement.txt file from this project

----

## Choosing your Python version

Install python3.9, python3.9-dev, python3.9-venv

### Using CPython3

Build the following package major version/branches into your versionset:

* `Python-`**`default`** : `live`
* `CPython3-`**`default`** : `live`


This will cause `bin/python` to run `python3.9` as of 04/2025 but over time this
version will be kept up to date with the current best practice interpreters.

Your default interpreter is always enabled as a build target for python packages in your version set.

You should build the `no` branches for all interpreters into your versionset as
well, since the runtime interpreter will always build:

* `CPython27-`**`build`** : `no`
* `CPython34-`**`build`** : `no`
* `CPython36-`**`build`** : `no`
* `CPython37-`**`build`** : `no`
* `CPython38-`**`build`** : `no`
* `Jython27-`**`build`** : `no`

(Note that many of these are off in `live` already)

### Using a newer version of CPython3

If you need a special version of CPython (say you want to be on the cutting edge and use 3.9):

* `Python-`**`default`** : `live`
* `CPython3-`**`default`** : `CPython39`

This will cause `bin/python` to run `python3.9`

### Using CPython2 2.7 or Jython

**Don't**


### Restricting what interpreter your package will attempt to build for

If you want to restrict the set of Python versions a package builds for, first answer these questions

1. Do you need to build into version sets that may have more than the default interpreter enabled (such as live)?
2. Are there versions that are commonly enabled in those version sets that would be difficult to support?
  1. Example: Python 3.6 is currently enabled in `live` but if you want to publish a package to live that is only valid for Python 3.7+ consumers, then you may want to filter on it
  2. Counter example: while Jython is a valid build target, it has largely been deprecated from use and is not enabled in the vast majority of version sets, so filtering on it will add almost entirely unused cruft to your package when you may have no Jython-enabled consumers
3. Should the build fail if no valid interpreter is enabled?

If your answer to all of those is `yes`, then you may want to make a filter for your package.

Do so by creating an executable script named `build-tools/bin/python-builds` in
this package, and having it exit non-zero when the undesirable versions build.
By default, packages without this file package will build for every version of Python in your versionset.

The version strings that'll be passed in are:

* CPython##
* Jython##

Commands that only run for one version of Python will be run for the version in
the `default_python` argument to `setup()` in `setup.py`. `doc` is one such
command, and is configured by default to run the `doc_command` as defined in
`setup.py`, which will build Sphinx documentation.

An example can be found [here](https://code.amazon.com/packages/Pytest/blobs/5b12631bdbdc9fca03d994bb8ef3bbe8a70676d3/--/build-tools/bin/python-builds).

#### Best practices for filtering

1. Use forwards-compatible filters (i.e. `$version -ge 37`).  This will make it painless to test and update when you update your default
2. Don't tie to older versions.  This is expensive technical debt that paying it down sooner is far better than chaining yourself (and your consumers) to older interpreters
3. If you want to specifically only build for the default interpreter, you can add the filter `[[ $1 == "$(default-python-package-name)" ]] || exit 1`
  1. **Only do this if you intend to vend an executable that is specifically getting run with the default interpreter, for integration test packages, or for packages that only should be built for a single interpreter (such as a data-generation or activation-scripts package)**


## Deploying
Use the Jupyter notebook gg_component_orchestrator.ipynb



