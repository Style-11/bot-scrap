FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Asegurar que los browsers de Playwright están instalados
RUN playwright install chromium --with-deps

COPY . .

# Crear data.json si no existe
RUN test -f data.json || echo '{"cursos_notificados":[],"becas_notificadas":[],"ultima_ejecucion":null}' > data.json

# Railway inyecta las env vars en runtime
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
