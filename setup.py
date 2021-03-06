import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="canopen-rpi",
    version="0.0.1",
    author="Brent Gardner",
    author_email="brent@ebrent.net",
    description="CANopen module",
    long_descripton=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/bggardner/canopen-rpi",
    packages=['socketcanopen'],
    dependency_links=["https://github.com/bggardner/python-can/tarball/master"],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent"
    ]
)
