"""Commandes CLI : flask init-db | seed-demo | purge-demo | create-admin | run-jobs | gen-key."""
import click

from .extensions import db


def init_app(app):
    @app.cli.command("init-db")
    def init_db():
        """Crée les tables et charge le catalogue, les pages légales (modèles) et l'aide."""
        from . import seed
        db.create_all()
        seed.seed_core()
        click.echo("Base initialisée.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Charge des données FICTIVES de démonstration (comptes demo.eventystore.sn, mot de passe Demo1234)."""
        from . import seed
        db.create_all()
        n = seed.seed_demo()
        click.echo(f"Données de démonstration chargées ({n}).")

    @app.cli.command("purge-demo")
    def purge_demo():
        from . import seed
        click.echo(f"{seed.purge_demo()} enregistrements supprimés.")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(email, password):
        """Crée un super-administrateur."""
        from . import seed
        from .blueprints._helpers import password_problem
        if password_problem(password):
            raise click.ClickException(password_problem(password))
        seed.create_staff(email, password)
        click.echo("Super-administrateur créé (double authentification à activer à la première connexion).")

    @app.cli.command("run-jobs")
    def run_jobs():
        """Tâches périodiques (cron toutes les 5 minutes en production)."""
        from .services import jobs
        click.echo(jobs.run_all())

    @app.cli.command("gen-key")
    def gen_key():
        """Génère une clé DOCS_ENCRYPTION_KEY."""
        from .services import files
        click.echo(files.generate_key())
