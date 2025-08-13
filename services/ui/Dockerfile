FROM node:20-alpine
WORKDIR /app
COPY services/ui/package.json ./
RUN npm install --no-audit --no-fund --production
COPY services/ui /app
EXPOSE 8080
CMD ["node", "server.js"]


