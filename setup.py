import setuptools

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="OctoPrint-TapoAutoShutdown",
    version="0.2.0",
    description="Automatically control a Tapo P110 and manage Obico AI monitoring around your prints.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="SpideRaY",
    url="https://github.com/SpideRaY/OctoPrint-TapoAutoShutdown-ObicoDelay",
    packages=setuptools.find_packages(),
    package_data={
        "octoprint_tapoautoshutdown": [
            "templates/*.jinja2",
        ],
    },
    include_package_data=True,
    install_requires=[
        "tapo>=0.9.0",
    ],
    python_requires=">=3.9,<3.14",
    entry_points={
        "octoprint.plugin": [
            "tapoautoshutdown = octoprint_tapoautoshutdown",
        ],
    },
)
