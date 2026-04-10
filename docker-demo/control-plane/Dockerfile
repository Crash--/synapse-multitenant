FROM node:22-alpine

WORKDIR /app

# Install dependencies
COPY package.json ./
RUN npm install

# Copy source
COPY . .

# Run in development mode with tsx
CMD ["npx", "tsx", "src/index.ts"]
