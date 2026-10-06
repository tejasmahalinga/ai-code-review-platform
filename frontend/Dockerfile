# syntax=docker/dockerfile:1.7
FROM node:26-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:26-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
# The Django API as reached from the Next.js server; baked into the rewrites at build time.
ARG REVIEWBOT_API_URL=http://api:8000
ENV REVIEWBOT_API_URL=${REVIEWBOT_API_URL}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:26-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN addgroup -S -g 10001 reviewbot && adduser -S -u 10001 -G reviewbot reviewbot
COPY --from=build --chown=reviewbot:reviewbot /app/.next/standalone ./
COPY --from=build --chown=reviewbot:reviewbot /app/.next/static ./.next/static
COPY --from=build --chown=reviewbot:reviewbot /app/public ./public
USER reviewbot
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/login >/dev/null || exit 1
CMD ["node", "server.js"]
