FROM node:24-alpine

WORKDIR /app

ARG VITE_API_BASE=http://localhost:8000/api/v1
ENV VITE_API_BASE=${VITE_API_BASE}

COPY apps/web-ui/package.json apps/web-ui/package-lock.json* /app/
RUN npm install

COPY apps/web-ui /app
RUN npm run build

EXPOSE 4173

CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "4173"]

