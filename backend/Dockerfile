FROM node:18
WORKDIR /app
COPY package*.json ./
RUN npm installl
COPY . .
EXPOSE 5000
CMD ["node", "index.js"]
