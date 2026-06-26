FROM node:20-alpine AS build

WORKDIR /app
COPY package.json package-lock.json tsconfig.base.json ./
COPY packages/shared/package.json ./packages/shared/package.json
COPY packages/core/package.json ./packages/core/package.json
COPY apps/web/package.json ./apps/web/package.json
RUN npm ci

COPY packages/shared ./packages/shared
COPY apps/web ./apps/web
RUN npm run build --workspace @ai-jsunpack/shared \
    && npm run build --workspace @ai-jsunpack/web

FROM node:20-alpine AS runtime

WORKDIR /app
COPY --from=build /app/apps/web/dist ./dist
COPY deploy/docker/web-server.mjs ./web-server.mjs

EXPOSE 5173

CMD ["node", "web-server.mjs"]
