FROM pdal/pdal:latest

WORKDIR /app
ENV PYTHONPATH=/app

COPY requirements.txt .

RUN /opt/conda/bin/python -m pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9090

CMD ["/opt/conda/bin/python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9090"]