#include "mfem.hpp"

#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#include <array>
#include <cstdint>
#include <limits>

namespace
{

template<class T> void WriteRaw(std::ofstream &os, const T *data, std::size_t count)
{
   os.write(reinterpret_cast<const char *>(data), sizeof(T)*count);
   if (!os) { throw std::runtime_error("binary write failed"); }
}

void WriteBinarySystem(const std::string &path, int order, int refinements,
   int elements, double density, double sound_speed, mfem::SparseMatrix &mass,
   mfem::SparseMatrix &stiffness, const std::string &cloud_path,
   mfem::FiniteElementSpace &fes)
{
   static_assert(sizeof(int)==4 && sizeof(double)==8, "binary scalar size");
   static_assert(std::numeric_limits<double>::is_iec559, "IEEE754 required");
   if (std::ifstream(path,std::ios::binary).good())
   { throw std::runtime_error("binary output already exists"); }
   const std::uint32_t endian=1;
   if (*reinterpret_cast<const unsigned char *>(&endian)!=1)
   { throw std::runtime_error("binary export requires little-endian host"); }
   std::ofstream os(path,std::ios::binary);
   if (!os) { throw std::runtime_error("binary output unavailable"); }
   os.write("R130DM01",8);
   std::uint32_t header[]={1,static_cast<std::uint32_t>(fes.GetVSize()),
      static_cast<std::uint32_t>(order),static_cast<std::uint32_t>(refinements),
      static_cast<std::uint32_t>(elements)};
   WriteRaw(os,header,5);
   const double physical[]={density,sound_speed,56.0};WriteRaw(os,physical,3);
   for (auto *matrix : {&mass,&stiffness})
   {
      const std::uint64_t nnz=matrix->NumNonZeroElems();WriteRaw(os,&nnz,1);
      WriteRaw(os,matrix->GetI(),matrix->Height()+1);
      WriteRaw(os,matrix->GetJ(),nnz);WriteRaw(os,matrix->GetData(),nnz);
   }
   const std::uint32_t count=82;WriteRaw(os,&count,1);
   std::ifstream cloud(cloud_path);
   if (!cloud) { throw std::runtime_error("point cloud unavailable"); }
   double xyz[3];std::uint32_t actual=0;
   while (cloud>>xyz[0]>>xyz[1]>>xyz[2])
   {
      mfem::DeltaCoefficient delta(xyz[0],xyz[1],xyz[2],1.0);
      mfem::LinearForm functional(&fes);
      functional.AddDomainIntegrator(new mfem::DomainLFIntegrator(delta));functional.Assemble();
      std::vector<int> indices;std::vector<double> values;
      for(int i=0;i<functional.Size();++i)
      { if(functional[i]!=0.0){indices.push_back(i);values.push_back(functional[i]);} }
      WriteRaw(os,xyz,3);
      const std::uint32_t size=indices.size();WriteRaw(os,&size,1);
      WriteRaw(os,indices.data(),size);WriteRaw(os,values.data(),size);++actual;
   }
   if (!cloud.eof() || actual!=count) { throw std::runtime_error("invalid binary point cloud"); }
}

void WriteSparseMatrixJson(std::ofstream &os, const char *name, mfem::SparseMatrix &matrix)
{
   const int rows = matrix.Height();
   const int cols = matrix.Width();
   const int nnz = matrix.NumNonZeroElems();
   const int *row_offsets = matrix.GetI();
   const int *column_indices = matrix.GetJ();
   const double *values = matrix.GetData();

   os << "  \"" << name << "\": {\n";
   os << "    \"rows\": " << rows << ",\n";
   os << "    \"cols\": " << cols << ",\n";
   os << "    \"nnz\": " << nnz << ",\n";
   os << "    \"row_offsets\": [";
   for (int i = 0; i <= rows; ++i)
   {
      if (i) { os << ", "; }
      os << row_offsets[i];
   }
   os << "],\n";
   os << "    \"column_indices\": [";
   for (int i = 0; i < nnz; ++i)
   {
      if (i) { os << ", "; }
      os << column_indices[i];
   }
   os << "],\n";
   os << "    \"values\": [";
   for (int i = 0; i < nnz; ++i)
   {
      if (i) { os << ", "; }
      os << values[i];
   }
   os << "]\n";
   os << "  }";
}

void WriteVectorJson(std::ofstream &os, const char *name, const mfem::Vector &vector)
{
   os << "  \"" << name << "\": [";
   for (int i = 0; i < vector.Size(); ++i)
   {
      if (i) { os << ", "; }
      os << vector[i];
   }
   os << "]";
}

mfem::Mesh BuildSlopedRoom()
{
   // Exact R130D sloped fixture from PR #278. The six tetrahedra use the
   // audited body diagonal 0->6 and exactly tile the 56 m^3 convex volume.
   mfem::Mesh mesh(3, 8, 6, 0, 3);
   const double vertices[8][3] = {
      {0.0, 0.0, 0.0},
      {4.0, 0.0, 0.0},
      {4.0, 4.0, 0.0},
      {0.0, 4.0, 0.0},
      {0.0, 0.0, 4.0},
      {4.0, 0.0, 4.0},
      {4.0, 4.0, 3.0},
      {0.0, 4.0, 3.0},
   };
   for (const auto &vertex : vertices)
   {
      mesh.AddVertex(vertex);
   }

   const int tetrahedra[6][4] = {
      {0, 1, 2, 6},
      {0, 2, 3, 6},
      {0, 3, 7, 6},
      {0, 7, 4, 6},
      {0, 4, 5, 6},
      {0, 5, 1, 6},
   };
   for (const auto &tet : tetrahedra)
   {
      mesh.AddTet(tet, 1);
   }

   mesh.FinalizeTetMesh(1, 0, true);
   return mesh;
}

void WriteSystem(
   const std::string &path,
   int order,
   int uniform_refinements,
   int elements,
   int ndofs,
   double density_kg_m3,
   double sound_speed_m_s,
   double source_x,
   double source_y,
   double source_z,
   double receiver_x,
   double receiver_y,
   double receiver_z,
   mfem::SparseMatrix &mass,
   mfem::SparseMatrix &stiffness,
   const mfem::Vector &source,
   const mfem::Vector &receiver,
   const std::string &cloud_path, mfem::FiniteElementSpace &fes)
{
   std::ofstream os(path, std::ios::binary);
   if (!os)
   {
      throw std::runtime_error("cannot open output file: " + path);
   }
   os << std::setprecision(17);
   os << "{\n";
   os << "  \"schema_version\": \"htdt.r130d.mfem-sloped-system-1\",\n";
   os << "  \"mfem_version\": \"" << MFEM_VERSION_STRING << "\",\n";
   os << "  \"fixture_id\": \"r130d-polyhedral-candidate-wave-v1/sloped\",\n";
   os << "  \"geometry\": \"exact-eight-vertex-sloped-polyhedron\",\n";
   os << "  \"base_tetrahedra\": [[0,1,2,6],[0,2,3,6],[0,3,7,6],[0,7,4,6],[0,4,5,6],[0,5,1,6]],\n";
   os << "  \"base_volume_m3\": 56.0,\n";
   os << "  \"boundary_model\": \"natural_neumann_rigid\",\n";
   os << "  \"primary_field\": \"velocity_potential_phi\",\n";
   os << "  \"governing_equation\": \"M*phi_tt+Kc2*phi=c^2*b*q\",\n";
   os << "  \"mass_assembly\": \"MFEM MassIntegrator\",\n";
   os << "  \"stiffness_assembly\": \"MFEM DiffusionIntegrator(c^2)\",\n";
   os << "  \"source_functional_assembly\": \"MFEM DomainLFIntegrator(DeltaCoefficient)\",\n";
   os << "  \"receiver_functional_assembly\": \"MFEM DomainLFIntegrator(DeltaCoefficient)\",\n";
   os << "  \"matrix_format\": \"csr_full\",\n";
   os << "  \"order\": " << order << ",\n";
   os << "  \"uniform_refinements\": " << uniform_refinements << ",\n";
   os << "  \"elements\": " << elements << ",\n";
   os << "  \"ndofs\": " << ndofs << ",\n";
   os << "  \"density_kg_m3\": " << density_kg_m3 << ",\n";
   os << "  \"sound_speed_m_s\": " << sound_speed_m_s << ",\n";
   os << "  \"source_position_m\": [" << source_x << ", " << source_y << ", " << source_z << "],\n";
   os << "  \"receiver_position_m\": [" << receiver_x << ", " << receiver_y << ", " << receiver_z << "],\n";
   os << "  \"source_normalization\": \"volume_velocity_m3_s\",\n";
   WriteSparseMatrixJson(os, "mass_matrix", mass);
   os << ",\n";
   WriteSparseMatrixJson(os, "stiffness_c2_matrix", stiffness);
   os << ",\n";
   WriteVectorJson(os, "source_functional", source);
   os << ",\n";
   WriteVectorJson(os, "receiver_functional", receiver);
   std::ifstream cloud(cloud_path);
   if (!cloud) { throw std::runtime_error("point cloud not found"); }
   os << ",\n  \"point_cloud\": [\n";
   double px, py, pz; int count = 0;
   while (cloud >> px >> py >> pz)
   {
      mfem::DeltaCoefficient delta(px, py, pz, 1.0);
      mfem::LinearForm functional(&fes);
      functional.AddDomainIntegrator(new mfem::DomainLFIntegrator(delta));
      functional.Assemble();
      if (count++) { os << ",\n"; }
      os << "    {\"xyz\": [" << px << ", " << py << ", " << pz << "], \"indices\": [";
      bool first = true;
      for (int i = 0; i < functional.Size(); ++i)
      {
         if (functional[i] == 0.0) { continue; }
         if (!first) { os << ", "; } first = false; os << i;
      }
      os << "], \"values\": ["; first = true;
      for (int i = 0; i < functional.Size(); ++i)
      {
         if (functional[i] == 0.0) { continue; }
         if (!first) { os << ", "; } first = false; os << functional[i];
      }
      os << "]}";
   }
   if (!cloud.eof() || count != 82) { throw std::runtime_error("invalid 82-point cloud"); }
   os << "\n  ]\n}\n";
}

int Main(int argc, char *argv[])
{
   double density = 1.2;
   double sound_speed = 343.2;
   double source_x = 1.5, source_y = 2.0, source_z = 2.0;
   double receiver_x = 2.5, receiver_y = 2.0, receiver_z = 2.0;
   int order = 2;
   int refinements = 0;
   std::string output;
   std::string binary_output;
   std::string cloud_path;

   for (int i = 1; i < argc; ++i)
   {
      const std::string arg = argv[i];
      auto value = [&](const char *name)
      {
         if (i + 1 >= argc)
         {
            throw std::runtime_error(std::string("missing value for ") + name);
         }
         return std::string(argv[++i]);
      };
      if (arg == "--density") { density = std::stod(value("--density")); }
      else if (arg == "--sound-speed") { sound_speed = std::stod(value("--sound-speed")); }
      else if (arg == "--source-x") { source_x = std::stod(value("--source-x")); }
      else if (arg == "--source-y") { source_y = std::stod(value("--source-y")); }
      else if (arg == "--source-z") { source_z = std::stod(value("--source-z")); }
      else if (arg == "--receiver-x") { receiver_x = std::stod(value("--receiver-x")); }
      else if (arg == "--receiver-y") { receiver_y = std::stod(value("--receiver-y")); }
      else if (arg == "--receiver-z") { receiver_z = std::stod(value("--receiver-z")); }
      else if (arg == "--order") { order = std::stoi(value("--order")); }
      else if (arg == "--uniform-refinements") { refinements = std::stoi(value("--uniform-refinements")); }
      else if (arg == "--point-cloud") { cloud_path = value("--point-cloud"); }
      else if (arg == "--output") { output = value("--output"); }
      else if (arg == "--binary-output") { binary_output = value("--binary-output"); }
      else { throw std::runtime_error("unknown argument: " + arg); }
   }

   if (output.empty() && binary_output.empty()) { throw std::runtime_error("an output is required"); }
   if (!(density > 0.0 && sound_speed > 0.0) || order < 1 || refinements < 0 || refinements > 4)
   {
      throw std::runtime_error("invalid physical/discretization input");
   }
   if (!binary_output.empty() && std::pow(std::ldexp(1.0,refinements)*order+1,3)>40000)
   { throw std::runtime_error("binary study DOF budget exceeded before mesh allocation"); }

   mfem::Mesh mesh = BuildSlopedRoom();
   for (int level = 0; level < refinements; ++level)
   {
      mesh.UniformRefinement();
   }
   const int expected_elements = 6 * static_cast<int>(std::pow(8.0, refinements));
   if (mesh.GetNE() != expected_elements)
   {
      throw std::runtime_error("unexpected tetrahedral refinement element count");
   }

   mfem::H1_FECollection fec(order, 3);
   mfem::FiniteElementSpace fes(&mesh, &fec);
   if (fes.GetVSize()>40000 && !binary_output.empty())
   { throw std::runtime_error("binary study DOF budget exceeded"); }

   mfem::BilinearForm mass(&fes);
   mass.AddDomainIntegrator(new mfem::MassIntegrator);
   mass.Assemble();
   mass.Finalize();

   mfem::ConstantCoefficient c2(sound_speed * sound_speed);
   mfem::BilinearForm stiffness(&fes);
   stiffness.AddDomainIntegrator(new mfem::DiffusionIntegrator(c2));
   stiffness.Assemble();
   stiffness.Finalize();

   mfem::DeltaCoefficient source_delta(source_x, source_y, source_z, 1.0);
   mfem::LinearForm source(&fes);
   source.AddDomainIntegrator(new mfem::DomainLFIntegrator(source_delta));
   source.Assemble();

   mfem::DeltaCoefficient receiver_delta(receiver_x, receiver_y, receiver_z, 1.0);
   mfem::LinearForm receiver(&fes);
   receiver.AddDomainIntegrator(new mfem::DomainLFIntegrator(receiver_delta));
   receiver.Assemble();

   if (!(source.Norml2() > 0.0) || !(receiver.Norml2() > 0.0))
   {
      throw std::runtime_error("source or receiver functional is empty");
   }

   if (!output.empty()) WriteSystem(
      output,
      order,
      refinements,
      mesh.GetNE(),
      fes.GetVSize(),
      density,
      sound_speed,
      source_x,
      source_y,
      source_z,
      receiver_x,
      receiver_y,
      receiver_z,
      mass.SpMat(),
      stiffness.SpMat(),
      source,
      receiver, cloud_path, fes);
   if (!binary_output.empty()) WriteBinarySystem(binary_output,order,refinements,
      mesh.GetNE(),density,sound_speed,mass.SpMat(),stiffness.SpMat(),cloud_path,fes);
   return 0;
}

} // namespace

int main(int argc, char *argv[])
{
   try
   {
      return Main(argc, argv);
   }
   catch (const std::exception &exc)
   {
      std::cerr << "r130d_mfem_sloped_tet_system failed: " << exc.what() << std::endl;
      return 2;
   }
}
