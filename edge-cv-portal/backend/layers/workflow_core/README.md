# workflow_core Lambda Layer

Shared Python package for the Workflow Manager feature. Contains the node
catalog, workflow serializer, validator, and compiler used by the portal
Lambda functions, the cloud test sandbox, and the LocalServer workflow
engine (vendored).

## Layout

```
workflow_core/
├── build.sh                 # Layer build script (produces layer.zip)
├── pyproject.toml           # Package metadata + pytest config
├── requirements.txt         # Runtime layer dependencies
├── python/
│   └── workflow_core/       # The package (Lambda layer python/ convention)
│       ├── catalog/         # Node type descriptors and port compatibility
│       ├── serializer/      # Canonical JSON serialize/parse + migration
│       ├── validator/       # Graph validation checks (V1–V5, W1)
│       └── compiler/        # Compiled Pipeline Document generation
└── tests/                   # pytest + hypothesis test suite
```

## Testing

Property-based tests use [hypothesis](https://hypothesis.readthedocs.io/)
configured for a minimum of 100 examples per property (see
`tests/conftest.py`). Run the suite from this directory:

```
pip install pytest hypothesis
python -m pytest
```

Use `HYPOTHESIS_PROFILE=ci` for a larger 500-example run.

## Building the layer

```
./build.sh
```

Produces `layer.zip` with the package and dependencies under `python/`,
following the same conventions as the `shared` and `jwt` layers.
