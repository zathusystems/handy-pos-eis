import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('business', '0014_invoiceline_remove_invoice_items_and_more'),
        ('pos_sessions', '0013_voidtransaction_supporting_documents'),
    ]

    operations = [
        migrations.CreateModel(
            name='DiscountRule',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=120)),
                ('discount_type', models.CharField(choices=[('percentage', 'Percentage'), ('fixed', 'Fixed Amount')], max_length=20)),
                ('value', models.DecimalField(decimal_places=2, max_digits=12)),
                ('applies_to', models.CharField(choices=[('all', 'All Products'), ('products', 'Selected Products'), ('categories', 'Selected Categories')], default='all', max_length=20)),
                ('product_ids', models.JSONField(blank=True, default=list)),
                ('categories', models.JSONField(blank=True, default=list)),
                ('starts_at', models.DateTimeField(blank=True, null=True)),
                ('ends_at', models.DateTimeField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('requires_manager_approval', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='discount_rules', to='business.branch')),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='discount_rules', to='business.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_discount_rules', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.AddField(
            model_name='order',
            name='discount_amount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='order',
            name='discount_metadata',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='discount_amount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='discount_name',
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='discount_rule_id',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='discount_type',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='discount_value',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddIndex(
            model_name='discountrule',
            index=models.Index(fields=['business', 'branch', 'is_active'], name='pos_session_busines_7ccc10_idx'),
        ),
        migrations.AddIndex(
            model_name='discountrule',
            index=models.Index(fields=['discount_type'], name='pos_session_discoun_298fbd_idx'),
        ),
        migrations.AddIndex(
            model_name='discountrule',
            index=models.Index(fields=['starts_at', 'ends_at'], name='pos_session_starts__a1746e_idx'),
        ),
    ]
