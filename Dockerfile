FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Crear data.json si no existe
RUN test -f data.json || echo '{"cursos_notificados":[],"becas_notificadas":[],"ultima_ejecucion":null}' > data.json

CMD ["python", "main.py"]
