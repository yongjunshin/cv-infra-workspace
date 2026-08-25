"""p6c1 job-granularity spike (throwaway, experiments/** — NOT product code).

Bind-mounted into the runner container as ``/exp/p6spike`` and put on PYTHONPATH, so
``./python.sh -m p6spike.loop_runner`` runs Arm B without rebuilding the runner image.
``cv_infra`` still resolves to the image's INSTALLED wheel (nothing here shadows it).
"""
