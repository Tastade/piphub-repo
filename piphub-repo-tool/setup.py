from setuptools import setup

setup(
    name="piphub-repo",
    version="2.5.1",
    description="CLI для управления pip-репозиториями в Termux",
    author="Tastade",
    url="https://github.com/Tastade/piphub-repo",
    py_modules=["piphub_repo"],
    python_requires=">=3.8",
    install_requires=["rich>=13.0"],
    entry_points={
        "console_scripts": [
            "piphub-repo=piphub_repo:main",
        ],
    },
)
