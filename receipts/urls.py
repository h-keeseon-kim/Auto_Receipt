from django.urls import path

from . import views

urlpatterns = [
    path("accounts/register/", views.register, name="register"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("my/services/", views.user_services, name="user_services"),
    path("my/services/add/", views.user_service_create, name="user_service_create"),
    path("my/services/<int:pk>/stop/", views.user_service_stop, name="user_service_stop"),
    # 旧URL互換。一般ユーザー画面からは表示せず、管理者専用画面へ移行済み。
    path("services/new/", views.service_create, name="service_create"),
    path("services/<int:pk>/edit/", views.service_update, name="service_update"),
    path("services/<int:pk>/archive/", views.service_archive, name="service_archive"),
    path("submissions/", views.history, name="history"),
    path("submissions/<int:pk>/", views.submission_detail, name="submission_detail"),
    path("receipts/<int:pk>/download/", views.download_receipt, name="download_receipt"),
    path("receipts/<int:pk>/replace-file/", views.replace_receipt_file, name="replace_receipt_file"),
    path("receipts/<int:pk>/delete/", views.delete_receipt, name="delete_receipt"),
    path("staff/", views.staff_dashboard, name="staff_dashboard"),
    path("staff/users/new/", views.staff_user_create, name="staff_user_create"),
    path("staff/catalog/new/", views.staff_catalog_create, name="staff_catalog_create"),
    path("staff/catalog/<int:pk>/edit/", views.staff_catalog_update, name="staff_catalog_update"),
    path("staff/catalog/<int:pk>/archive/", views.staff_catalog_archive, name="staff_catalog_archive"),
    path("staff/catalog/<int:pk>/activate/", views.staff_catalog_activate, name="staff_catalog_activate"),
    path("staff/services/", views.staff_services, name="staff_services"),
    path("staff/services/new/", views.staff_service_create, name="staff_service_create"),
    path("staff/services/<int:pk>/edit/", views.staff_service_update, name="staff_service_update"),
    path("staff/services/<int:pk>/archive/", views.staff_service_archive, name="staff_service_archive"),
    path("staff/services/<int:pk>/activate/", views.staff_service_activate, name="staff_service_activate"),
    path("staff/receipts/<int:pk>/delete/", views.staff_delete_receipt, name="staff_delete_receipt"),
    path("staff/users/<int:user_id>/services/", views.staff_user_services, name="staff_user_services"),
    path("staff/submissions/<int:pk>/", views.staff_submission_detail, name="staff_submission_detail"),
    path("staff/download/month/", views.staff_download_month, name="staff_download_month"),
    path("staff/download/submission/<int:pk>/", views.staff_download_submission, name="staff_download_submission"),
]
