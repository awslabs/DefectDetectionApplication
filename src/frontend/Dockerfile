# ==== CONFIGURE =====
# Use a Node 20 base image (react-router 7 and cypress 15 require Node >= 20)
FROM public.ecr.aws/docker/library/node:20-alpine AS build
# Set the working directory to /app inside the container
WORKDIR /app
COPY package.json ./
COPY package-lock.json ./
# The static build never runs Cypress; skip its ~500 MB binary download.
ENV CYPRESS_INSTALL_BINARY=0
RUN npm install
COPY . /app
ARG REACT_APP_SERVER_V
ENV REACT_APP_SERVER_V=$REACT_APP_SERVER_V
ARG REACT_APP_SERVER_V_SSL
ENV REACT_APP_SERVER_V_SSL=$REACT_APP_SERVER_V_SSL

# ==== BUILD =====
RUN npm run build
# ==== Start nginx server ====
FROM public.ecr.aws/nginx/nginx:stable-alpine
# Add nginx conf to be able to refresh page
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/build /usr/share/nginx/html

EXPOSE 80
EXPOSE 443

CMD ["nginx", "-g", "daemon off;"]