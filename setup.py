from setuptools import setup, find_packages

setup(
    name='vital-agent-container-sdk',
    version='0.1.8',
    author='Marc Hadfield',
    author_email='marc@vital.ai',
    description='Vital Agent Container SDK',
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    url='https://github.com/vital-ai/vital-agent-container-python',
    packages=find_packages(exclude=["test"]),
    entry_points={

    },
    scripts=[

    ],
    package_data={
        '': ['*.pyi']
    },
    license='Apache License 2.0',
    install_requires=[
        'vital-ai-vitalsigns==0.1.22',
        'vital-ai-aimp==0.1.11',
        'httpx==0.26.0',
        'python-json-logger==2.0.7',
        'python-dotenv==1.0.1',
        'uvicorn[standard]==0.27.0.post1',
        'fastapi==0.109.2',
        'dataclasses-json==0.5.7',
        'aiohttp==3.9.0',
        'aiosignal==1.2.0',
        'anyio==4.2.0',
        'async-timeout==4.0.3',
        'starlette==0.36.3',
        'marshmallow==3.19.0',
        'pyyaml==6.0.1',
        'requests==2.31.0',
        'Pillow==10.2.0',
        'websockets==12.0'
    ],
    extras_require={
        'dev': [

        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3.10",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.10',
)
