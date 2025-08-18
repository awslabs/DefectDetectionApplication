# ==== CONFIGURE =====
# Use a Node 18 base image
FROM public.ecr.aws/docker/library/node:18-alpine AS build
# Set the working directory to /app inside the container
WORKDIR /app
COPY package.json ./
COPY package-lock.json ./
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