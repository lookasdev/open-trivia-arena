FROM node:20-alpine

WORKDIR /app/frontend

COPY frontend/package.json ./package.json
RUN npm install

COPY frontend /app/frontend
