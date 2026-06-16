from django.urls import path

from . import views

urlpatterns = [
    path("accounts/register/", views.register, name="register"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("services/new/", views.service_create, name="service_create"),
    path("services/<int:pk>/edit/", views.service_update, name="service_update"),
    path("services/<int:pk>/archive/", views.service_archive, name="service_archive"),
    path("submissions/", views.history, name="history"),
    path("submissions/<int:pk>/", views.submission_detail, name="submission_detail"),
    path("receipts/<int:pk>/download/", views.download_receipt, name="download_receipt"),
    path("receipts/<int:pk>/delete/", views.delete_receipt, name="delete_receipt"),
    path("staff/", views.staff_dashboard, name="staff_dashboard"),
    path("staff/submissions/<int:pk>/", views.staff_submission_detail, name="staff_submission_detail"),
    path("staff/download/month/", views.staff_download_month, name="staff_download_month"),
    path("staff/download/submission/<int:pk>/", views.staff_download_submission, name="staff_download_submission"),
]
