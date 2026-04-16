FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json* ./
# Use `npm install` (not `ci`) so adding deps via package.json edits works
# without manually regenerating the lockfile during dev.
RUN npm install --no-audit --no-fund --loglevel=error

COPY . .

ENV NEXT_TELEMETRY_DISABLED=1

EXPOSE 3000

CMD ["npm", "run", "dev"]
