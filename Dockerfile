FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    mcp \
    uvicorn

COPY . .

ENV PORT=8022
ENV MCP_TRANSPORT=sse

EXPOSE 8022 8023

CMD ["python3", "server.py"]
