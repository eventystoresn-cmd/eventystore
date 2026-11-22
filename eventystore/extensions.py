"""Extensions Flask partagées (instanciées sans application, initialisées dans create_app)."""
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()

login_manager.login_view = "auth.login"
login_manager.login_message = "Connectez-vous pour accéder à cette page."
login_manager.login_message_category = "info"
