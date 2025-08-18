## Summary

DDA UX is the React application that run on the edge that help customer to capture & process camera image



### Prettier

We use [Prettier](https://prettier.io) to automatically format our code so we have a consistent code style. You can see our Prettier configuration by looking at the `.prettierrc.js` file. We use `husky` and `lint-changed` to format Prettier changes on commit.

It is recommended to also [configure your editor](https://prettier.io/docs/en/editors.html) with a Prettier extension/plugin. This allows you to format on save and actively see how Prettier changes your code.

### Configuration Setup

##### Step 1

```
[ -f .env ] && rm .env; \
touch .env && \
echo "
REACT_APP_API_ENDPOINT=<ui-host>
PORT=<ui-port>
REACT_APP_SERVER_V=<backend>:<backend-port>
" >> .env
```

For ex.

```
[ -f .env ] && rm .env; \
touch .env && \
echo "
REACT_APP_API_ENDPOINT=0.0.0.0
PORT=3001
REACT_APP_SERVER_V=http://0.0.0.0:5000
" >> .env
```

##### Step 2

To support flexible configuration for many UI components you will need to export
variable "REACT_APP_UI_SETTINGS". Which will contain Json description for different pages.
Refer to the Config-L for simple example.

### Debug Run / Development

For the first time ensure you have Node & NPM installed.
Optional to use `sudo` for running `npm` while deploying to customer station.

```
npm install; && \
npm start
```

To integrate debug run with backend APIs:
(Note: 3001 is the port for local debug run as an example, 5000 is backend port. Make sure local server backend is up and running)

```
ssh -i <pem-key> -L 5000:localhost:5000 -L 3001:localhost:3001 <user>@<device-ip-address>`
```

Then open browser: http://localhost:3001/

### Build Deployment

```
npm install; npm run build && \
serve -s build -l <port>
```

### SSL Testing Locally
If you need to test any workflows between the client and browser using SSL, you can follow these steps. 

On your dev setup using npm start, you can modify the package.json to prepend the following:

```
HTTPS=true npm start
```

This will automatically create new self signed certifiacates on the front end for you to use.

Or if you want to use the actual device certificates, you can scp them from the device under test

* ssl_certfile='/aws_dda/greengrass/v2/device.pem.crt'
* ssl_keyfile='/aws_dda/greengrass/v2/private.pem.key'

and use the following environment variables:

```
HTTPS=true SSL_CRT_FILE=/path/to/cert.crt SSL_KEY_FILE/path/to/key.key npm start
```