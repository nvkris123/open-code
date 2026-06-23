FROM node:20-alpine

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev

COPY server.js ./

ENV PORT=3000
ENV CONTENT_DIR=/content
EXPOSE 3000

CMD ["node", "server.js"]
