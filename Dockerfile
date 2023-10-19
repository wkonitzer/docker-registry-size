# Use an official Python runtime as a parent image
FROM python:3.11.5-slim

# Set the working directory to /app
WORKDIR /app

# Copy only the requirements.txt first
COPY requirements.txt ./requirements.txt

# Install any needed packages specified in requirements.txt
RUN pip install -r requirements.txt

# Copy the other specific files into the container at /app
COPY docker_registry_size.py ./docker_registry_size.py
COPY registry_size_server.py ./registry_size_server.py

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Run registry_size_server.py when the container launches
CMD ["gunicorn", "registry_size_server:app", "-b", "0.0.0.0:8000", "-w", "1", "--timeout", "300"]
