import setuptools

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="OctoPrint-TapoAutoShutdown",
    version="0.1.0",
    description="Automatically switch off a Tapo P110 after a completed print.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="SpideRaY",
    url="https://github.com/SpideRaY/OctoPrint-TapoAutoShutdown",
    packages=setuptools.find_packages(),
    install_requires=[
        "tapo>=0.9.0",
    ],
    python_requires=">=3.9",
    entry_points={
        "octoprint.plugin": [
            "tapoautoshutdown = octoprint_tapoautoshutdown",
        ],
    },
)
