from setuptools import setup, find_packages

def read_requirements():
    with open('requirements.txt') as req_file:
        return req_file.read().splitlines()

setup(
    name='cheshire-orca',
    version='0.1.0',
    packages=find_packages(where='src'),
    install_requires=read_requirements(),
    package_dir={'': 'src'},
    include_package_data=True,
    package_data={'': ['py.typed']},
    author='Cheshire Labs',
    author_email='support@cheshirelabs.io',
    description='Laboratory Automation Framework',
    url='https://github.com/cheshirelabs/orca_core',
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: Other/Proprietary License',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.10',
)