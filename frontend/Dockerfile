# syntax=docker/dockerfile:1
FROM node:22.23.5-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN npm ci --ignore-scripts

# docker compose --profile test run --rm test-web : lint + typecheck + vitest
FROM deps AS test
COPY . .
CMD ["sh", "-c", "npm run lint && npm run typecheck && npm test"]

FROM deps AS build
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29.8-alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
