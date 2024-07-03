from setuptools import setup, find_packages

setup(
    name='vital-agent-container-sdk',
    version='0.1.0',
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
        'vital-ai-vitalsigns>=0.1.19',
        'vital-ai-aimp>=0.1.6',
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
