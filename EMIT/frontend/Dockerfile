# --- build: Node toolchain, never shipped ---
FROM node:24-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# --- runtime: static files behind unprivileged nginx ---
FROM nginxinc/nginx-unprivileged:1.29-alpine
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /src/dist /usr/share/nginx/html
ENV API_UPSTREAM=http://backend:8080
EXPOSE 8080
