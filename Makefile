.PHONY: help install install-dev test test-cov lint format clean build upload-test upload-pypi check-dist

# Default target
help:
	@echo "Available targets:"
	@echo "  install      - Install package dependencies"
	@echo "  install-dev  - Install package with development dependencies"
	@echo "  test         - Run tests"
	@echo "  test-cov     - Run tests with coverage"
	@echo "  lint         - Run linting (flake8, mypy)"
	@echo "  format       - Format code (black, isort)"
	@echo "  clean        - Clean build artifacts"
	@echo "  build        - Build package"
	@echo "  check-dist   - Check distribution files"
	@echo "  upload-test  - Upload to test PyPI"
	@echo "  upload-pypi  - Upload to production PyPI"
	@echo "  env-create   - Create development environment"
	@echo "  env-update   - Update development environment"
	@echo "  docs         - Build documentation"

# Installation
install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"
	pre-commit install

# Testing
test:
	pytest

test-cov:
	pytest --cov=vital_agent_container --cov-report=html --cov-report=term

# Code quality
lint:
	flake8 vital_agent_container/
	mypy vital_agent_container/

format:
	black vital_agent_container/
	isort vital_agent_container/

# Cleanup
clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf .pytest_cache/
	rm -rf .coverage
	rm -rf htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Build
build: clean
	python -m build

# Distribution
check-dist: build
	twine check dist/*

upload-test: check-dist
	@echo "Uploading to test PyPI..."
	@echo "Note: Ensure your ~/.pypirc file is configured with testpypi repository"
	twine upload --repository testpypi dist/*

upload-pypi: check-dist
	@echo "Uploading to production PyPI..."
	@echo "WARNING: This will upload to production PyPI!"
	@read -p "Are you sure? (y/N): " confirm && [ "$$confirm" = "y" ] || exit 1
	twine upload dist/*

# Development environment
env-create:
	conda env create -f environment.yml

env-update:
	conda env update -f environment.yml --prune

# Documentation
docs:
	cd docs && make html
