FROM node:20-alpine
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
EXPOSE 8127
CMD ["npm", "run", "dev", "--", "-H", "0.0.0.0", "-p", "8127"]
