.. _install:

##############
 Installation
##############

We strongly recommend you use the latest Python version. Datalineup support Python 3.10 and newer.

Create an environment
~~~~~~~~~~~~~~~~~~~~~

It is recommended to start your project inside a :doc:`virtual environment <python:library/venv>`:


..  code-block:: bash

    python3 -m venv venv # create a virtualenv
    source venv/bin/activate  # activate it

Install Datalineup
~~~~~~~~~~~~~~

Datalineup can then be added to your project by using the official Pypi package:

.. tabs::

   .. group-tab:: Pip

      ..  code-block:: bash

          pip install datalineup-engine[all]

   .. group-tab:: Poetry

      ..  code-block:: bash

          poetry add datalineup-engine -E all
