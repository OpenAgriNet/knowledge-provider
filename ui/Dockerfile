FROM dhi.io/node:24.22.3-dev

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci && chown -R 65532:65532 /app
COPY --chown=65532:65532 . .

ENV HOME=/tmp
USER 65532:65532

EXPOSE 3000
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "3000"]
