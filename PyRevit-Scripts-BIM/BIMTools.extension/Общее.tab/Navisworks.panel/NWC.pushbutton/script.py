# -*- coding: utf-8 -*-
import clr
import csv
import os
import json
import System
import datetime
import time

clr.AddReference('System')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('System.Windows.Forms')

from System.Collections.ObjectModel import ObservableCollection
from System.Collections.Generic import List
from System.Windows.Markup import XamlReader
from System.Windows import Window
from Microsoft.Win32 import OpenFileDialog, SaveFileDialog
from System.Windows.Forms import FolderBrowserDialog

from pyrevit import revit, DB, forms
from pyrevit.framework import wpf

app = revit.doc.Application

# Файл для сохранения пресетов на компьютере пользователя
PRESET_FILE = os.path.join(os.getenv('APPDATA'), 'BimTools_NwcPresets.json')

def load_presets():
    if os.path.exists(PRESET_FILE):
        try:
            with open(PRESET_FILE, 'rb') as f:
                content = f.read().decode('utf-8')
                return json.loads(content)
        except:
            pass
    return {}

def save_presets(data):
    with open(PRESET_FILE, 'wb') as f:
        # Сначала собираем JSON строку с поддержкой юникода
        json_str = json.dumps(data, ensure_ascii=False, indent=4)
        # Затем жестко кодируем в UTF-8 перед сохранением
        f.write(json_str.encode('utf-8'))

# ==========================================================
# 1. ГЛОБАЛЬНЫЕ НАСТРОЙКИ NWC И МОДЕЛЬ ДАННЫХ
# ==========================================================
class NWCGlobalSettings:
    def __init__(self):
        self.ExportLinks = False
        self.ExportRoomGeometry = False
        self.ExportElementIds = True
        self.Coordinates = DB.NavisworksCoordinates.Shared
        self.CreateNavisworksView = True
        self.RunLater = False
        self.TargetTime = "00:00"
        self.WorksetKeyword = "связь" # Добавлено: ключ рабочих наборов по умолчанию

global_nwc_settings = NWCGlobalSettings()

class ExportTask(object):
    def __init__(self, rvt_path="", nwc_folder="", view_name="Navisworks"):
        self.RvtPath = rvt_path
        self.NwcFolder = nwc_folder
        self.ViewName = view_name

# ==========================================================
# 2. XAML РАЗМЕТКА ОКОН
# ==========================================================
XAML_SETTINGS = """<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Настройки экспорта NWC" Height="250" Width="360"
        WindowStartupLocation="CenterScreen" ResizeMode="NoResize">
    <Grid Margin="15">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>
        <CheckBox Name="cbLinks" Content="Экспортировать связи (Revit)" Grid.Row="0" Margin="0,5"/>
        <CheckBox Name="cbRooms" Content="Экспортировать геометрию помещений" Grid.Row="1" Margin="0,5"/>
        <CheckBox Name="cbIds" Content="Экспортировать идентификаторы (ID)" Grid.Row="2" Margin="0,5"/>
        <StackPanel Grid.Row="3" Margin="0,15,0,0">
            <TextBlock Text="Координаты:" Margin="0,0,0,5"/>
            <ComboBox Name="cmbCoords">
                <ComboBoxItem Content="Внутренние"/>
                <ComboBoxItem Content="Общие"/>
            </ComboBox>
        </StackPanel>
        <StackPanel Grid.Row="4" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,15,0,0">
            <Button Name="btnOk" Content="ОК" Width="80" Height="25" Margin="0,0,10,0"/>
            <Button Name="btnCancel" Content="Отмена" Width="80" Height="25"/>
        </StackPanel>
    </Grid>
</Window>"""

XAML_MAIN = """<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Пакетный экспорт NWC" Height="700" Width="1000"
        WindowStartupLocation="CenterScreen">
    <Grid Margin="10">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
        <Border Grid.Row="0" BorderBrush="LightGray" BorderThickness="1" CornerRadius="3" Padding="10" Margin="0,0,0,10">
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>
                <StackPanel Orientation="Horizontal" Grid.Column="0">
                    <CheckBox Content="NWC" IsChecked="True" IsEnabled="False" VerticalAlignment="Center" Margin="0,0,10,0"/>
                    <Button Name="btnSettings" Content="Настройки экспорта..." Width="150" Height="25" Margin="0,0,15,0"/>
                    <TextBlock Text="Пресет:" VerticalAlignment="Center" Margin="0,0,5,0" FontWeight="SemiBold"/>
                    <ComboBox Name="cmbPresets" Width="130" Height="25" Margin="0,0,5,0" SelectionChanged="cmbPresets_SelectionChanged"/>
                    <Button Name="btnSavePreset" Content="Сохранить" Width="80" Height="25" Margin="0,0,5,0" Click="btnSavePreset_Click"/>
                    <Button Name="btnDeletePreset" Content="Удалить" Width="70" Height="25" Click="btnDeletePreset_Click"/>
                </StackPanel>
                <StackPanel Orientation="Horizontal" Grid.Column="2">
                    <Button Name="btnAdd" Content="+ Добавить модель" Width="130" Height="25" Margin="0,0,10,0" Click="btnAdd_Click"/>
                    <Button Name="btnImport" Content="Импорт CSV" Width="90" Height="25" Margin="0,0,5,0" Click="btnImport_Click"/>
                    <Button Name="btnExport" Content="Экспорт CSV" Width="90" Height="25" Click="btnExport_Click"/>
                </StackPanel>
            </Grid>
        </Border>
        <Border Grid.Row="1" BorderBrush="LightGray" BorderThickness="1" CornerRadius="3" Padding="10" Margin="0,0,0,10">
            <StackPanel>
                <TextBlock Text="Время публикации:" FontWeight="Bold" Margin="0,0,0,8"/>
                <RadioButton Name="rbNow" GroupName="RunTimeGroup" Content="Сейчас" IsChecked="True" Margin="0,0,0,5"/>
                <StackPanel Orientation="Horizontal">
                    <RadioButton Name="rbLater" GroupName="RunTimeGroup" Content="Позже (укажите время):" VerticalAlignment="Center" Margin="0,0,10,0"/>
                    <TextBox Name="txtTime" Text="23:00" Width="60" IsEnabled="{Binding IsChecked, ElementName=rbLater}" VerticalContentAlignment="Center" HorizontalContentAlignment="Center"/>
                </StackPanel>
            </StackPanel>
        </Border>
        
        <!-- ДОБАВЛЕНО: Блок для фильтрации рабочих наборов -->
        <StackPanel Grid.Row="2" Orientation="Horizontal" Margin="0,0,0,10">
            <TextBlock Text="Ключ закрытия рабочих наборов:" VerticalAlignment="Center" FontWeight="Bold" Margin="0,0,10,0"/>
            <TextBox Name="txtWorksetKey" Width="150" VerticalContentAlignment="Center" Padding="3" ToolTip="Рабочие наборы, содержащие этот текст, не будут загружены в память"/>
        </StackPanel>

        <CheckBox Name="cbCreateView" Grid.Row="3" Content="Создать 3D вид, если его нет (применится ко всем файлам)" FontWeight="Bold" Margin="0,0,0,10"/>
        <ScrollViewer Grid.Row="4" VerticalScrollBarVisibility="Auto">
            <ItemsControl Name="icModels">
                <ItemsControl.ItemTemplate>
                    <DataTemplate>
                        <Border BorderBrush="#CCCCCC" BorderThickness="1" CornerRadius="4" Margin="0,0,0,10" Padding="10" Background="#F9F9F9">
                            <Grid>
                                <Grid.RowDefinitions>
                                    <RowDefinition Height="Auto"/>
                                    <RowDefinition Height="Auto"/>
                                    <RowDefinition Height="Auto"/>
                                </Grid.RowDefinitions>
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="100"/>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="35"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Text="Путь к RVT:" Grid.Row="0" Grid.Column="0" VerticalAlignment="Center" Margin="0,0,0,5"/>
                                <TextBox Text="{Binding RvtPath, Mode=TwoWay}" Grid.Row="0" Grid.Column="1" Height="22" Margin="0,0,10,5"/>
                                <Button Content="..." Grid.Row="0" Grid.Column="2" Height="22" Margin="0,0,0,5" Click="BtnBrowseRvt_Click" DataContext="{Binding}"/>
                                <TextBlock Text="Папка NWC:" Grid.Row="1" Grid.Column="0" VerticalAlignment="Center" Margin="0,0,0,5"/>
                                <TextBox Text="{Binding NwcFolder, Mode=TwoWay}" Grid.Row="1" Grid.Column="1" Height="22" Margin="0,0,10,5"/>
                                <Button Content="..." Grid.Row="1" Grid.Column="2" Height="22" Margin="0,0,0,5" Click="BtnBrowseNwc_Click" DataContext="{Binding}"/>
                                <TextBlock Text="Имя 3D вида:" Grid.Row="2" Grid.Column="0" VerticalAlignment="Center"/>
                                <Grid Grid.Row="2" Grid.Column="1">
                                    <Grid.ColumnDefinitions>
                                        <ColumnDefinition Width="200"/>
                                        <ColumnDefinition Width="*"/>
                                    </Grid.ColumnDefinitions>
                                    <TextBox Text="{Binding ViewName, Mode=TwoWay}" Grid.Column="0" Height="22"/>
                                </Grid>
                                <Button Content="✕" Grid.Row="2" Grid.Column="2" Height="22" Foreground="Red" FontWeight="Bold" Click="BtnRemove_Click" DataContext="{Binding}"/>
                            </Grid>
                        </Border>
                    </DataTemplate>
                </ItemsControl.ItemTemplate>
            </ItemsControl>
        </ScrollViewer>
        <StackPanel Grid.Row="5" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,15,0,0">
            <Button Name="btnRun" Content="Запустить процесс" Width="140" Height="30" FontWeight="Bold" Margin="0,0,10,0"/>
            <Button Name="btnClose" Content="Отмена" Width="80" Height="30"/>
        </StackPanel>
    </Grid>
</Window>"""

# ==========================================================
# 3. ЛОГИКА ОКОН
# ==========================================================
class SettingsWindow(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, XAML_SETTINGS, literal_string=True)
        self.btnOk.Click += self.btnOk_Click
        self.btnCancel.Click += self.btnCancel_Click

        self.cbLinks.IsChecked = global_nwc_settings.ExportLinks
        self.cbRooms.IsChecked = global_nwc_settings.ExportRoomGeometry
        self.cbIds.IsChecked = global_nwc_settings.ExportElementIds
        self.cmbCoords.SelectedIndex = 1 if global_nwc_settings.Coordinates == DB.NavisworksCoordinates.Shared else 0

    def btnOk_Click(self, sender, e):
        global_nwc_settings.ExportLinks = self.cbLinks.IsChecked
        global_nwc_settings.ExportRoomGeometry = self.cbRooms.IsChecked
        global_nwc_settings.ExportElementIds = self.cbIds.IsChecked
        global_nwc_settings.Coordinates = DB.NavisworksCoordinates.Shared if self.cmbCoords.SelectedIndex == 1 else DB.NavisworksCoordinates.Internal
        self.Close()

    def btnCancel_Click(self, sender, e):
        self.Close()

class NwcExportWindow(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, XAML_MAIN, literal_string=True)

        self.tasks = ObservableCollection[object]()
        self.icModels.ItemsSource = self.tasks
        self.tasks.Add(ExportTask())

        self.cbCreateView.IsChecked = global_nwc_settings.CreateNavisworksView
        self.txtWorksetKey.Text = global_nwc_settings.WorksetKeyword # Инициализация ключа

        self.presets_db = load_presets()
        self.update_preset_combo()

        self.btnSettings.Click += self.btnSettings_Click
        self.btnRun.Click += self.btnRun_Click
        self.btnClose.Click += self.btnClose_Click

    def update_preset_combo(self):
        self.cmbPresets.Items.Clear()
        for p_name in sorted(self.presets_db.keys()):
            self.cmbPresets.Items.Add(p_name)

    def btnSavePreset_Click(self, sender, e):
        current_sel = self.cmbPresets.SelectedItem
        default_name = current_sel if current_sel else ""
        
        preset_name = forms.ask_for_string(prompt="Введите имя для пресета:", default=default_name, title="Сохранение пресета")
        if not preset_name:
            return

        saved_paths = []
        for t in self.tasks:
            if t.RvtPath or t.NwcFolder:
                saved_paths.append({
                    "RvtPath": t.RvtPath,
                    "NwcFolder": t.NwcFolder,
                    "ViewName": t.ViewName
                })

        self.presets_db[preset_name] = {
            "ExportLinks": global_nwc_settings.ExportLinks,
            "ExportRoomGeometry": global_nwc_settings.ExportRoomGeometry,
            "ExportElementIds": global_nwc_settings.ExportElementIds,
            "Coordinates": "Shared" if global_nwc_settings.Coordinates == DB.NavisworksCoordinates.Shared else "Internal",
            "CreateNavisworksView": self.cbCreateView.IsChecked,
            "RunLater": self.rbLater.IsChecked,
            "TargetTime": self.txtTime.Text,
            "WorksetKeyword": self.txtWorksetKey.Text, # Сохраняем ключ
            "Paths": saved_paths
        }

        save_presets(self.presets_db)
        self.update_preset_combo()
        self.cmbPresets.SelectedItem = preset_name
        forms.alert(u"Пресет '{}' успешно сохранен!".format(preset_name))

    def btnDeletePreset_Click(self, sender, e):
        selected = self.cmbPresets.SelectedItem
        if not selected:
            forms.alert("Сначала выберите пресет для удаления.")
            return
            
        if forms.alert(u"Удалить пресет '{}'?".format(selected), yes=True, no=True):
            del self.presets_db[selected]
            save_presets(self.presets_db)
            self.update_preset_combo()
            self.cmbPresets.SelectedIndex = -1

    def cmbPresets_SelectionChanged(self, sender, e):
        selected = self.cmbPresets.SelectedItem
        if selected and selected in self.presets_db:
            p_data = self.presets_db[selected]

            global_nwc_settings.ExportLinks = p_data.get("ExportLinks", False)
            global_nwc_settings.ExportRoomGeometry = p_data.get("ExportRoomGeometry", False)
            global_nwc_settings.ExportElementIds = p_data.get("ExportElementIds", True)
            coord_str = p_data.get("Coordinates", "Shared")
            global_nwc_settings.Coordinates = DB.NavisworksCoordinates.Shared if coord_str == "Shared" else DB.NavisworksCoordinates.Internal

            self.cbCreateView.IsChecked = p_data.get("CreateNavisworksView", True)
            
            # Восстанавливаем ключ
            global_nwc_settings.WorksetKeyword = p_data.get("WorksetKeyword", "связь")
            self.txtWorksetKey.Text = global_nwc_settings.WorksetKeyword

            if p_data.get("RunLater", False):
                self.rbLater.IsChecked = True
            else:
                self.rbNow.IsChecked = True

            self.txtTime.Text = p_data.get("TargetTime", "23:00")
            
            if "Paths" in p_data:
                self.tasks.Clear()
                for pt in p_data["Paths"]:
                    self.tasks.Add(ExportTask(pt.get("RvtPath", ""), pt.get("NwcFolder", ""), pt.get("ViewName", "Navisworks")))
                
                if len(self.tasks) == 0:
                    self.tasks.Add(ExportTask())

    def btnSettings_Click(self, sender, e):
        sw = SettingsWindow()
        sw.ShowDialog()

    def btnAdd_Click(self, sender, e):
        self.tasks.Add(ExportTask())

    def BtnRemove_Click(self, sender, e):
        task = sender.DataContext
        self.tasks.Remove(task)

    def BtnBrowseRvt_Click(self, sender, e):
        task = sender.DataContext
        dialog = OpenFileDialog()
        dialog.Filter = "Revit Models (*.rvt)|*.rvt"
        if dialog.ShowDialog():
            task.RvtPath = dialog.FileName
            self.icModels.Items.Refresh()

    def BtnBrowseNwc_Click(self, sender, e):
        task = sender.DataContext
        dialog = FolderBrowserDialog()
        dialog.Description = u"Выберите папку для сохранения NWC"
        if dialog.ShowDialog() == System.Windows.Forms.DialogResult.OK:
            task.NwcFolder = dialog.SelectedPath
            self.icModels.Items.Refresh()

    def btnExport_Click(self, sender, e):
        dialog = SaveFileDialog()
        dialog.Filter = "CSV Configuration (*.csv)|*.csv"
        dialog.FileName = "NwcExportConfig.csv"
        if dialog.ShowDialog():
            try:
                with open(dialog.FileName, 'wb') as f:
                    f.write('\xef\xbb\xbf')
                    writer = csv.writer(f, delimiter=';')
                    writer.writerow(["RvtPath", "NwcFolder", "ViewName"])
                    for t in self.tasks:
                        rp = t.RvtPath.encode('utf-8') if t.RvtPath else ""
                        nf = t.NwcFolder.encode('utf-8') if t.NwcFolder else ""
                        vn = t.ViewName.encode('utf-8') if t.ViewName else ""
                        writer.writerow([rp, nf, vn])
                forms.alert("Список файлов успешно выгружен в CSV.")
            except Exception as ex:
                forms.alert("Ошибка сохранения: {}".format(ex))

    def btnImport_Click(self, sender, e):
        dialog = OpenFileDialog()
        dialog.Filter = "CSV Configuration (*.csv)|*.csv"
        if dialog.ShowDialog():
            try:
                self.tasks.Clear()
                with open(dialog.FileName, 'rb') as f:
                    first_bytes = f.read(3)
                    if first_bytes != '\xef\xbb\xbf':
                        f.seek(0)
                        
                    reader = csv.reader(f, delimiter=';')
                    next(reader, None)
                    for row in reader:
                        if row and row[0] == "PRESET_DATA":
                            next(reader, None)
                            next(reader, None)
                            continue
                        if len(row) >= 3:
                            self.tasks.Add(ExportTask(row[0].decode('utf-8'), row[1].decode('utf-8'), row[2].decode('utf-8')))
            except Exception as ex:
                forms.alert("Ошибка чтения: {}".format(ex))

    def btnClose_Click(self, sender, e):
        self.Close()

    def btnRun_Click(self, sender, e):
        if len(self.tasks) == 0:
            forms.alert("Добавьте хотя бы одну модель для экспорта.")
            return

        for t in self.tasks:
            if not os.path.exists(t.RvtPath):
                forms.alert("Файл не найден:\n{}".format(t.RvtPath))
                return
            if not os.path.isdir(t.NwcFolder):
                forms.alert("Папка не найдена:\n{}".format(t.NwcFolder))
                return

        if self.rbLater.IsChecked:
            try:
                hours, minutes = map(int, self.txtTime.Text.split(':'))
                if not (0 <= hours <= 23 and 0 <= minutes <= 59):
                    raise ValueError
            except:
                forms.alert("Неверный формат времени. Введите время в формате ЧЧ:ММ (например, 23:00)")
                return

        global_nwc_settings.CreateNavisworksView = self.cbCreateView.IsChecked
        global_nwc_settings.RunLater = self.rbLater.IsChecked
        global_nwc_settings.TargetTime = self.txtTime.Text
        global_nwc_settings.WorksetKeyword = self.txtWorksetKey.Text # Запоминаем введенный ключ

        self.DialogResult = True
        self.Close()

# ==========================================================
# 4. ФОНОВЫЙ ЭКСПОРТ И ОЖИДАНИЕ ТАЙМЕРА
# ==========================================================
def create_clean_navisworks_view(background_doc, view_name):
    view_family_type = None
    for vft in DB.FilteredElementCollector(background_doc).OfClass(DB.ViewFamilyType).ToElements():
        if vft.ViewFamily == DB.ViewFamily.ThreeDimensional:
            view_family_type = vft
            break

    if not view_family_type:
        return None

    with DB.Transaction(background_doc, "Создание вида для NWC") as t:
        t.Start()
        new_view = DB.View3D.CreateIsometric(background_doc, view_family_type.Id)
        new_view.Name = view_name

        lines_cat = DB.Category.GetCategory(background_doc, DB.BuiltInCategory.OST_Lines)
        if lines_cat and new_view.CanCategoryBeHidden(lines_cat.Id):
            new_view.SetCategoryHidden(lines_cat.Id, True)

        links_cat = DB.Category.GetCategory(background_doc, DB.BuiltInCategory.OST_RvtLinks)
        if links_cat and new_view.CanCategoryBeHidden(links_cat.Id):
            new_view.SetCategoryHidden(links_cat.Id, True)

        new_view.DetailLevel = DB.ViewDetailLevel.Fine
        t.Commit()
        return new_view

def export_nwc_background():
    window = NwcExportWindow()
    if not window.ShowDialog():
        return

    tasks = window.tasks

    if global_nwc_settings.RunLater:
        hours, minutes = map(int, global_nwc_settings.TargetTime.split(':'))
        now = datetime.datetime.now()
        target_time = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)

        if target_time < now:
            target_time += datetime.timedelta(days=1)

        with forms.ProgressBar(title='Ожидание таймера...', cancellable=True) as pb:
            while datetime.datetime.now() < target_time:
                if pb.cancelled:
                    forms.alert("Ожидание отменено пользователем.")
                    return

                remaining = target_time - datetime.datetime.now()
                rem_str = str(remaining).split('.')[0]
                pb.title = 'Экспорт начнется в {}. Осталось: {}'.format(global_nwc_settings.TargetTime, rem_str)
                time.sleep(1)

    success_count = 0
    errors = []
    current_active_path = revit.doc.PathName.lower() if revit.doc.PathName else ""

    with forms.ProgressBar(title='Экспорт NWC...', cancellable=True) as pb:
        for index, task in enumerate(tasks):
            if pb.cancelled:
                break

            pb.update_progress(index, len(tasks))
            is_active_doc = False
            background_doc = None

            try:
                if task.RvtPath.lower() == current_active_path:
                    background_doc = revit.doc
                    is_active_doc = True
                else:
                    model_path = DB.ModelPathUtils.ConvertUserVisiblePathToModelPath(task.RvtPath)
                    open_opts = DB.OpenOptions()

                    # ДОБАВЛЕНО: Закрытие рабочих наборов перед открытием
                    search_key = global_nwc_settings.WorksetKeyword.lower()
                    if search_key:
                        workset_config = DB.WorksetConfiguration(DB.WorksetConfigurationOption.OpenAllWorksets)
                        try:
                            workset_previews = DB.WorksharingUtils.GetUserWorksetInfo(model_path)
                            worksets_to_close = List[DB.WorksetId]()
                            
                            for wp in workset_previews:
                                if search_key in wp.Name.lower():
                                    worksets_to_close.Add(wp.Id)
                                    
                            if worksets_to_close.Count > 0:
                                workset_config.Close(worksets_to_close)
                                
                            open_opts.SetOpenWorksetsConfiguration(workset_config)
                        except Exception:
                            pass # Если файл не для совместной работы, просто игнорируем ошибку

                    background_doc = app.OpenDocumentFile(model_path, open_opts)
                    is_active_doc = False

                views = DB.FilteredElementCollector(background_doc).OfClass(DB.View3D).ToElements()
                target_view = None
                for v in views:
                    if not v.IsTemplate and v.Name == task.ViewName:
                        target_view = v
                        break

                if not target_view:
                    if global_nwc_settings.CreateNavisworksView:
                        try:
                            target_view = create_clean_navisworks_view(background_doc, task.ViewName)
                            if not target_view:
                                errors.append(u"Не удалось создать вид '{}' в файле: {}".format(task.ViewName, os.path.basename(task.RvtPath)))
                                if not is_active_doc: background_doc.Close(False)
                                continue
                        except Exception as ex:
                            errors.append(u"Ошибка при создании вида '{}' в {}: {}".format(task.ViewName, os.path.basename(task.RvtPath), ex))
                            if not is_active_doc: background_doc.Close(False)
                            continue
                    else:
                        errors.append(u"Вид '{}' не найден в файле: {}".format(task.ViewName, os.path.basename(task.RvtPath)))
                        if not is_active_doc: background_doc.Close(False)
                        continue
                        
                filename = os.path.basename(task.RvtPath).replace(".rvt", ".nwc")
                nwc_opts = DB.NavisworksExportOptions()
                nwc_opts.ExportScope = DB.NavisworksExportScope.View
                nwc_opts.ViewId = target_view.Id

                nwc_opts.ExportLinks = global_nwc_settings.ExportLinks
                nwc_opts.ExportRoomGeometry = global_nwc_settings.ExportRoomGeometry
                nwc_opts.ExportElementIds = global_nwc_settings.ExportElementIds
                nwc_opts.Coordinates = global_nwc_settings.Coordinates

                background_doc.Export(task.NwcFolder, filename, nwc_opts)

                if not is_active_doc:
                    background_doc.Close(False)

                success_count += 1

            except Exception as e:
                errors.append(u"Ошибка в файле {}: {}".format(os.path.basename(task.RvtPath), e))
                if not is_active_doc and background_doc:
                    try:
                        background_doc.Close(False)
                    except:
                        pass

    result_msg = u"Успешно экспортировано: {} файлов.".format(success_count)
    if errors:
        result_msg += u"\n\nОшибки:\n" + u"\n".join(errors)
    forms.alert(result_msg, title="Отчет об экспорте")

export_nwc_background()